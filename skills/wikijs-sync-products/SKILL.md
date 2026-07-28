---
name: wikijs-sync-products
description: Install, upgrade, repair, or remove the PV Wiki n8n automation on a user-authorized VPS. Discover an existing n8n or deploy a dedicated instance, deploy the internal PV Wiki worker, collect user-selected search/AI/Wiki/database settings, import inactive workflows, and run acceptance checks. Do not use this skill as the recurring product updater and do not create a Hermes cron job.
version: 0.3.0
author: sulirang
license: MIT
platforms:
  - linux
  - macos
  - windows
metadata:
  hermes:
    tags:
      - installer
      - n8n
      - wikijs
      - postgresql
      - exa
      - knowledge-base
    requires_toolsets:
      - terminal
---

# Install PV Wiki automation

This is an installation and lifecycle runbook. Hermes may discover, install,
configure, validate, upgrade, repair, or remove the deployment. Once accepted,
n8n owns recurring schedules and the PV Wiki worker owns product processing.
Hermes must stop; it must not run the maintenance cycle itself.

Before changing a server, read:

- [the n8n deployment guide](../../deploy/n8n/README.md);
- [Wiki.js setup](references/wikijs-setup.md);
- [database boundaries](references/database-schema.md);
- [operations and rollback](references/operations.md).

## Non-negotiable boundaries

- Work only on the exact VPS and URL authorized by the user. Do not scan
  unrelated hosts or address ranges.
- Perform read-only discovery first. Do not install, restart, import, activate,
  or overwrite anything until the target instance and ownership are clear.
- Never create a Hermes cron, heartbeat, background loop, or recurring task.
- Do not enable n8n Execute Command, mount the Docker socket, or expose an
  arbitrary shell/API operation.
- Do not put Exa, AI, PostgreSQL, Wiki.js, or worker secrets in workflow
  JSON, Git, command-line arguments, or logs.
- Do not make the source catalogue writable. n8n, Wiki.js, and the product
  catalogue must not share application tables or database users.
- Import workflows inactive. Only the user may approve publishing their
  schedules after the acceptance run.
- Do not delete volumes, state, workflows, Wiki.js pages, or credentials as an
  implied part of rollback or uninstall.

## 1. Establish scope

Collect these decisions without asking the user to paste long-lived secrets
into chat:

- VPS SSH host, port, account, and the allowed directory/service boundary;
- expected n8n URL, domain/DNS status, and whether an existing reverse proxy is
  in scope;
- whether n8n is shared or dedicated and who owns it;
- final Wiki.js path prefix and private/unpublished acceptance visibility (or
  an explicitly separate staging state volume);
- backup location and retention;
- user-selected OpenAI-compatible `AI_BASE_URL`, `AI_MODEL`, and a secure way
  to install `AI_API_KEY`;
- Exa keys, read-only catalogue settings, and restricted Wiki.js
  API token.

If the host, instance owner, or allowed deployment boundary is ambiguous, stop
and ask. Never infer authority from the fact that SSH happens to work.

## 2. Discover n8n read-only

Inspect only the authorized VPS. Typical read-only checks are:

```bash
docker ps --format '{{.Names}}\t{{.Image}}\t{{.Ports}}\t{{.Status}}'
docker compose ls
systemctl status n8n --no-pager
ss -ltnp
```

Also inspect likely Compose/service definitions only when their paths were
identified by the user or the preceding process listing. For a supplied n8n
URL, check `/healthz/readiness`; this verifies database readiness rather than
only process reachability.

Record:

- n8n version/image and URL;
- Docker Compose project or systemd service;
- PostgreSQL/storage and `/home/node/.n8n` persistence;
- encryption-key backup status;
- reverse proxy/TLS;
- Docker network the worker could join;
- existing workflows with the same PV Wiki names;
- owner and whether the instance is shared.

If multiple plausible instances exist, report them and ask the user to choose.
If an existing instance is unhealthy, unowned, unsupported, or lacks a backup,
do not modify it.

## 3. Choose the deployment path

### Reuse an existing n8n

Before importing anything:

1. Export/backup any workflow with a conflicting name.
2. Confirm the existing n8n version can import the bundled node versions.
3. Deploy only `pv-wiki-worker` and its persistent state on a network reachable
   by that chosen n8n. For Docker n8n, use
   `deploy/n8n/compose.existing-n8n.yaml` with the exact existing external
   network. For host/systemd n8n, use `compose.systemd-n8n.yaml`, keep port
   8080 on loopback, and change the three workflow URLs to
   `http://127.0.0.1:8080/...`. Keep port 8080 unbound from public interfaces.
4. Preserve the existing n8n database, encryption key, users, proxy, and
   unrelated workflows.

### Install a dedicated n8n

Use the pinned Compose bundle under `deploy/n8n`. Ask for deployment approval
after presenting the discovered “no usable instance” result. Then:

1. Confirm Docker Engine, Compose v2, Git, DNS/proxy prerequisites, and an
   exact checked-out PV Wiki revision. Record the revision. Create `.env` and
   `worker.env` from their examples with mode `0600`.
2. Generate separate random values for the n8n database password, n8n
   encryption key, and worker bearer token.
3. Keep n8n PostgreSQL, n8n data, and PV Wiki state in separate persistent
   volumes.
4. Start with `compose.bootstrap.yaml`, bind n8n to loopback, and have the user
   create the owner account through an SSH tunnel before any public route is
   enabled. Reuse the authorized HTTPS proxy, or use the Caddy overlay only
   after confirming DNS and ports 80/443 are free.
5. Keep `N8N_BLOCK_ENV_ACCESS_IN_NODE=true` and the risky Execute Command/local
   file trigger nodes excluded.
6. Start with the exact image versions recorded in the install manifest.

Never launch a second n8n next to an existing one merely because its login is
unknown.

## 4. Configure the worker

The user chooses every external provider. Install these values in
`worker.env`, never in n8n workflow JSON:

```dotenv
EXA_API_KEYS=...
AI_BASE_URL=https://provider.example/v1
AI_API_KEY=...
AI_MODEL=...
WIKIJS_URL=https://wiki.example.com
WIKIJS_TOKEN=...
PGHOST=...
PGDATABASE=...
PGUSER=...                # read-only
PGPASSWORD=...
PGSSLMODE=verify-full
PGSSLROOTCERT=/run/pv-wiki/catalogue-ca.pem
PV_WIKI_TRUSTED_SOURCE_DOMAINS_JSON=  # optional override/fast path
PV_WIKI_RESEARCH_MAX_ROUNDS=3
PV_WIKI_RESEARCH_MAX_QUERIES=7
PV_WIKI_RESEARCH_MAX_CREDITS=20
PV_WIKI_RESEARCH_MAX_SECONDS=600
PV_WIKI_WORKER_TOKEN=...  # separate 32+ character random value
```

Prefer `PGSSLMODE=verify-full`. The worker also accepts the other standard
libpq modes when selected explicitly. Use `disable`, `allow`, or `prefer` only
when the catalogue server cannot use TLS and the user accepts that credentials
and catalogue data may cross the network unencrypted. `PGSSLROOTCERT` is
required for `verify-ca`/`verify-full`; non-verifying modes ignore the mounted
path.

The supported AI protocol is OpenAI-compatible Chat Completions. The worker
adds `/chat/completions` to `AI_BASE_URL`. HTTPS is mandatory by default.
Loopback HTTP is accepted; another trusted private HTTP endpoint requires the
operator to explicitly set `AI_ALLOW_INSECURE_HTTP=true`. The optional
`AI_THINKING_MODE=enabled|disabled` sends the provider extension
`"thinking":{"type":"..."}` only when configured; leave it empty for
compatibility, or select `disabled` when a supported reasoning-by-default
model would otherwise exhaust the bounded output allowance before returning
JSON. Optional `AI_REASONING_EFFORT=high|max` is also sent only when
configured; DeepSeek documents `high` as its shortest supported effort. The
model receives
bounded public identity, search discovery hints without result URLs, and
extracted evidence; it
does not receive product database IDs, family codes, lease tokens, or
credentials by default. When a catalogue brand matches the operator-owned
supplier registry, the research prompt also receives only its public
manufacturer alias and bounded official hostnames so a configured exact-model
manufacturer extract does not trigger redundant independent corroboration.
Local validation, never AI output, still authorizes the source. An operator may
explicitly enable
`PV_WIKI_SEARCH_INCLUDE_INTERNAL_HINTS` only when alphanumeric product IDs and
brand codes are safe public search hints. It discovers manufacturer and product
type and may classify
matching generic hardware as out of scope. The local decision gate, not the model,
controls Wiki.js writes. Within one `/run-one`, the model may request at most
two supplemental passes using a fixed evidence-gap enum and locally validated
queries bound to the exact product model. It cannot provide a domain allowlist,
change the configured budgets, or call Wiki.js. `PV_WIKI_TRUSTED_SOURCE_DOMAINS_JSON` is an optional
public-manufacturer alias override and verification fast path, not a complete
registry that an operator must maintain. Without a match, the local gate may
automatically verify only an HTTPS manufacturer host whose name is consistent
with the AI-discovered manufacturer and whose bounded extract contains both the
manufacturer and complete catalogue model. A second independent HTTPS extract
must corroborate that identity. Every retained fact needs an exact quote from
the verified primary datasheet, but not from the corroborating identity source.
Failure becomes
`source_unverified`: it is audited and retried automatically, without creating
a product issue or manual-review task. The model cannot grant trust by itself.
For certificate-verifying PostgreSQL
modes, mount the catalogue public/private CA from `CATALOGUE_CA_PATH`
read-only as documented in the deployment guide.
The verified primary must be an original manufacturer PDF that the runtime
successfully downloaded and parsed, and its bounded extract must contain the
catalogue-bound full model. A manufacturer HTML page, regulatory/authorized
copy, or mirror may help discovery or identity corroboration but cannot grant
automatic publication. Unrelated extracted discovery candidates are ignored by
the publication identity check. The document may cover sibling models in the
same series and does not need to contain only the target. Each retained
specification must
include either a short exact target-model-only span or an exact
Markdown/TSV/normalized-PDF table header and parameter row that bind the value
to the same target column. Omit an unsafe parameter row. There is no minimum
fact count once the trusted primary datasheet and target membership pass.

Run `pv-wiki doctor` before any live probes, then `pv-wiki doctor --live`. The
doctor validates AI and search-provider configuration but intentionally spends
no AI or Exa credits.

## 5. Import n8n workflows inactive

Import:

- `PV Wiki - Product Cycle`;
- `PV Wiki - Homepage Refresh`.

Do not add secrets to the exported JSON. In the n8n UI, have the user create a
Header Auth credential named `PV Wiki Worker`:

```text
Header name: Authorization
Header value: Bearer <PV_WIKI_WORKER_TOKEN>
```

Attach that credential to all four HTTP Request nodes. Configure the user's
chosen n8n Error Workflow/notification channel only for systemic or batch-level
failures. Normal product non-publish outcomes must not create alerts or issue
tickets. Imports stay inactive until acceptance is complete.

The product workflow calls fixed internal endpoints:

```text
Monthly/Manual Trigger
  → POST /sync-catalogue
  → POST /run-one
  → IF processed=true, loop to POST /run-one

Hourly Due Recovery
  → POST /run-one
  → IF processed=true, loop to POST /run-one
Daily Catalogue Recovery
  -> POST /refresh-catalogue
  -> POST /run-one
  -> IF processed=true, loop to POST /run-one
```

The monthly product trigger runs at 08:05 Asia/Shanghai (00:05 UTC) on day 1,
preserving the first-day quota-reset cadence. A daily 03:17 refresh retries
catalogue ingestion without waking quota-paused products. The hourly trigger
resumes due and backoff work without rerunning the catalogue sync. The homepage workflow calls
`POST /publish-home` daily at 02:35 Asia/Shanghai. Catalogue/home calls use
bounded idempotent retries. `Run One Product` uses three retry-safe calls as a
batch circuit breaker and a 45-minute timeout: leases, the product-operation lock, and the worker's
per-action request ledger prevent duplicate mutation or blind paid-call
replays, while consecutive provider/system failures stop one workflow batch.
If the hourly trigger overlaps a still-running batch, `/run-one` returns the
clean stop reason `worker_busy` instead of creating a false workflow failure.
Catalogue sync has a separate serialized lock, so the monthly refresh can
overlap a product batch; a source change invalidates and reschedules the active
lease. Catalogue writes and the final source-check/Wiki-mutation/outcome
sequence share a publication fence, so a locally applied source revision cannot
be inserted midway through publication. Homepage publication also has its own
serialized lock and targets a different Wiki path. This is a single-process
fence: keep exactly one worker replica and do not run mutating CLI commands
concurrently with the scheduled HTTP worker.
The queue loops only when the response says `processed=true`. A no-due result
or exhaustion of every configured selected-provider key ends the loop. There is no shell
node and no arbitrary request body.

Legal non-publish results, including `no_datasheet`, `ambiguous`,
`insufficient_identity`, `out_of_scope`, and runtime `source_unverified`, are
silently stored in the audit and retried by the queue. They do not require a
person to review one issue per product. Provider, configuration, database, or
service failures that stop the workflow remain batch-level alerts.

When the user adds or replaces an Exa key, update `EXA_API_KEYS`, recreate the
worker so it receives the new
environment, then run
`Manual / New Key Start` once to wake quota-paused products immediately and
validate the key. Otherwise their first-of-next-UTC-month wake time remains.
HTTP 429 is a transient request-rate limit. Exa budget exhaustion uses HTTP
402 and rotates to the next configured key. Known partial work
before an explicit quota/4xx response is audited and remains retryable; only
ambiguous paid requests are replay-suppressed.

Catalogue ingestion is history-preserving: absence from a later source snapshot
does not automatically archive/delete a product or remove it from the homepage.
Do not infer destructive retirement without an explicit user policy.

## 6. Acceptance

Run these checks in order:

1. n8n `/healthz/readiness` returns 200.
2. Worker `/healthz` returns 200 from the n8n network.
3. `pv-wiki doctor --live` confirms the selected PostgreSQL mode, a read-only
   catalogue transaction, and Wiki.js access. Record any unencrypted-transport
   warning.
4. Run exactly one private/unpublished product directly with
   `pv-wiki run-one --worker-id manual-acceptance` at the final prefix. Do not
   start the looping n8n workflow until this check passes. If a different
   staging prefix is mandatory, use separate worker state and plan an explicit
   migration.
5. Confirm the full target model is present in the trusted official datasheet
   as either a standalone or series member, the complete original PDF is linked
   in references, every displayed specification is bound to the target, no
   internal family code appears on the page, and no review claim is invented.
6. Edit text outside `PV-WIKI-AUTO`, rerun when due during acceptance, and
   confirm the human text remains byte-for-byte intact.
7. Run the homepage workflow and confirm brand/category indexes, total count,
   per-brand counts, and recent products.
8. Inspect the n8n execution and PostgreSQL audit. They must not contain API
   keys or full extracted documents.
9. Run `docker compose --env-file .env -f compose.yaml exec -T n8n n8n audit`
   (or the equivalent command for the discovered instance) and confirm
   Execute Command remains unavailable.

Present the results. The user must explicitly approve publishing the two n8n
workflows.

## 7. Handoff

Write an install manifest without secret values:

- deployment mode: reused or dedicated n8n;
- n8n URL, version/image, Compose/service path, and owner;
- worker image/source revision and network;
- workflow names and IDs;
- credential names only;
- AI base URL and model, but not its key;
- state and backup locations;
- staging test product, result, and Wiki path;
- activation status and next scheduled time;
- rollback commands.

Report that recurring ownership has transferred to n8n, then stop.

## Upgrade, repair, and removal

For upgrades, export workflows and back up all persistent stores first. Review
n8n release notes, change one pinned image/runtime revision at a time, import
workflows inactive if they must be replaced, and repeat acceptance before
reactivation.

For repair, diagnose before changing. Preserve active leases and the
PostgreSQL state; do not “fix” a queue by deleting it.

For removal on a shared n8n, deactivate/export/remove only the PV Wiki
workflows and revoke their worker credential. Do not stop shared services. For
a dedicated stack, stopping containers while retaining volumes is the default
reversible action. Removing volumes/state, deleting Wiki.js pages, or revoking
external credentials each requires separate explicit approval.
