# Hermes PV Wiki operations

Use this runbook for the version 0.4 Hermes path. Use the n8n deployment only
for an explicitly chosen legacy rollback.

## Contents

- [Runtime ownership](#runtime-ownership)
- [Starting the MCP servers](#starting-the-mcp-servers)
- [Manual catalogue refresh](#manual-catalogue-refresh)
- [Recurring session order](#recurring-session-order)
- [Completion and publication recovery](#completion-and-publication-recovery)
- [Exa key-pool operations](#exa-key-pool-operations)
- [Monitoring and incident handling](#monitoring-and-incident-handling)
- [Backups and upgrades](#backups-and-upgrades)
- [Legacy rollback](#legacy-rollback)

## Runtime ownership

Run one serialized Hermes cron job. Hermes owns the research plan and session;
it uses `mcp-exa-pool` for public-web access and `mcp-pv-wiki` for durable
product state and Wiki.js publication.

The PV Wiki MCP deliberately has no product claim or lease. Its selector is a
read followed by an anti-join against completed `product_id` values. Two
overlapping sessions could therefore research the same unfinished product
before either saves it. Keep the Hermes job non-overlapping. The completion
primary key still ensures that only the first finished result is accepted.

Do not run the legacy n8n product cycle at the same time. Do not enable Hermes
native web, browser, terminal, delegation, or Exa `agent_run` for the
recurring job. The allowed toolsets are only `mcp-exa-pool` and
`mcp-pv-wiki`.

The separate `mcp-pv-wiki-catalogue-admin` toolset is interactive only. Never
attach it to cron or invoke it based on product/source content.

The new path does not use the legacy three-round, seven-query, five-URL,
20-credit, or 600-second limits. It also ignores the old global PV Wiki credit
budgets, leases, backoff queue, provider circuits, and `research_actions`
uncertain-action ledger. Do not monitor those fields as readiness signals for
Hermes research.

## Starting the MCP servers

Prefer the bundled stdio definitions in
`deploy/hermes/mcp-config.yaml.example`. Stdio keeps the services on the same
host and avoids a network listener:

```text
pv-wiki-exa-mcp
pv-wiki-research-mcp
pv-wiki-catalogue-mcp
```

The Exa process needs its Exa-key path, the independent receipt-key path, and
an optional transport timeout:

```dotenv
EXA_API_KEYS_FILE=/run/secrets/pv-wiki-exa-keys
PV_WIKI_EVIDENCE_HMAC_KEY_FILE=/run/secrets/pv-wiki-evidence-hmac-key
EXA_HTTP_TIMEOUT_SECONDS=60
```

The recurring PV Wiki process needs the same receipt-key secret, its dedicated
state database, and restricted Wiki.js settings. It receives no source
PostgreSQL settings or credentials and must not receive Exa or AI credentials.
The Exa process must not receive catalogue, state-database, or Wiki.js
credentials.

The catalogue-admin process needs `PV_WIKI_STATE_DATABASE_URL` plus the fixed,
non-secret `PGHOST`, `PGPORT`, `PGDATABASE`, `PGSSLMODE`, and optional
`PGSSLROOTCERT`. Do not configure `PGUSER` or `PGPASSWORD` on any long-running
Hermes process.

All three servers support explicitly opt-in Streamable HTTP:

```bash
pv-wiki-exa-mcp --transport streamable-http --host 127.0.0.1 --port 8000
pv-wiki-research-mcp --transport streamable-http --host 127.0.0.1 --port 8001
pv-wiki-catalogue-mcp --transport streamable-http --host 127.0.0.1 --port 8002
```

Their corresponding environment variables are `EXA_MCP_TRANSPORT`,
`EXA_MCP_HOST`, `EXA_MCP_PORT`; `PV_WIKI_MCP_TRANSPORT`, `PV_WIKI_MCP_HOST`,
`PV_WIKI_MCP_PORT`; and `PV_WIKI_CATALOGUE_MCP_TRANSPORT`,
`PV_WIKI_CATALOGUE_MCP_HOST`, `PV_WIKI_CATALOGUE_MCP_PORT`. A non-loopback bind
is refused unless the matching `*_MCP_ALLOW_REMOTE=true` escape hatch is set.
Use that escape hatch only behind authenticated private-network controls; none
of the servers implements a public Internet boundary by itself.

Do not put raw Exa keys in an HTTP header, URL, Hermes configuration, or MCP
argument. Even with HTTP transport, the upstream keys remain inside the Exa
MCP service and are read from `EXA_API_KEYS_FILE`.

The receipt HMAC secret is separate from all Exa keys and contains at least 32
random bytes. Under stdio, the Exa and research MCPs can read one owner-only file. With
separate service accounts, use two owner-only secret-manager mounts containing
the same receipt secret. Receipts are stateless over normalized URL and exact
content SHA-256; they require no table, timestamp, lease, retry, or ledger.

## Manual catalogue refresh

Use the `pv-wiki-refresh-catalogue` skill only after the user explicitly asks
to update the product list. The admin tool accepts exactly `username` and
`password`; the source host/database/TLS target remains operator-configured so
model output cannot turn the tool into an arbitrary database connector.

The values are passed directly as psycopg keyword arguments for one connection.
PV Wiki never writes them to a file, environment variable, state table, MCP
result, or service log. They can still remain in Hermes/provider conversation
or tool-call history. Tell the user to create a short-lived role restricted to
`SELECT` on `public.products`, call `pv_refresh_catalogue` once, and revoke the
role immediately after success or failure. Never echo either value or retry a
lost/failed call without a new user decision.

The source scan runs in a repeatable-read, read-only transaction and closes the
connection after the full result is materialized. Empty and duplicate-id scans
are rejected. The state update atomically replaces
`product_catalogue_snapshot`; absent source products leave that active snapshot
without deleting legacy attempts or completions. The result reports generation,
checksum, and added/changed/removed/unchanged counts without credentials.

Removing a product from the active snapshot does not cancel an already
accepted pending publication. That completion retains its validated product
payload and remains recoverable; handle withdrawal as a separate explicit
operator action.

## Recurring session order

Require every fresh Hermes session to execute this sequence:

1. Call `pv_pending_publication`.
2. If it returns a completion, call `pv_publish_result` for that product and
   end the run after the publication result is known.
3. Otherwise call the zero-argument `pv_next_product`. It reads only the active
   state-database snapshot.
4. Stop cleanly on `catalogue_not_loaded` and request an explicit interactive
   refresh. Stop normally on `no_unresearched_product`.
5. Research the returned product using only `web_search_exa`,
   `web_search_advanced_exa`, and `web_fetch_exa`.
6. Call `pv_save_research` with the exact returned `product_id` and
   `source_hash`, a schema-version-`2` decision, and the extracted evidence
   documents.
7. Treat `created=false` as already complete. Do not overwrite or research it
   again.
8. For a newly stored publishable completion, call `pv_publish_result` with
   its `product_id`. For a non-publish outcome, finish normally.

`pv_save_research` validates public URLs, decision structure, citations, and
exact evidence quotes before storage. A source-hash mismatch means the
catalogue changed after selection. Do not force the stale result into state;
start a later fresh session and select the current snapshot.

## Completion and publication recovery

The completion row is written before any Wiki.js mutation. A publishable row
with `published_at IS NULL` is pending publication and is excluded from new
research exactly like every other completion.

Recover pending work through the MCP tool or the independent publisher:

```bash
pv-wiki-publish-researched
pv-wiki-publish-researched --product-id PRODUCT_ID
```

The first form selects the oldest pending publication. The command prints one
JSON result and returns non-zero for an operational failure. It never invokes
Exa or an AI model.

If Wiki.js fails before the durable marker is written, leave the completion
unchanged. The next call upserts the same managed Wiki.js section and then
sets `published_at`, `wiki_path`, and `wiki_action`. An already published
completion returns success without another page mutation.

Do not delete a completion to repair publication. Fix Wiki.js connectivity,
permissions, path settings, or content validation, then rerun the publisher.
Delete a completion only when the user explicitly authorizes re-research of
that product after reviewing a database backup and the existing Wiki page.
Deletion is the only normal mechanism that makes a completed `product_id`
eligible again; catalogue updates do not do so automatically.

At completion-store initialization, legacy rows with
`products.last_success_at IS NOT NULL` and no existing completion are
backfilled as completed and already published. `ON CONFLICT DO NOTHING`
protects concurrent initialization, and a native Hermes completion is never
overwritten. Verify the counts before enabling the first Hermes cron.

## Exa key-pool operations

Store one or more authorized Exa keys in the file named by
`EXA_API_KEYS_FILE`, separated by commas or whitespace. Require mode `0600`
and ownership by the Exa MCP service account. The code retains
`EXA_API_KEYS`/`EXA_API_KEY` fallback support for legacy migrations, but do not
use or document raw environment keys in a production Hermes deployment.

Rotate the receipt secret only during a drained maintenance window: a receipt
issued before rotation cannot be verified afterward. Update both MCP copies
atomically and restart both services. This is a secret rotation procedure, not
a reason to store receipt state or retry a research action automatically.

The in-memory pool advances after every acquisition, so successful requests
use true round-robin selection. One request may try another healthy key only
after a definitive credential-specific response:

| Exa result | Pool action |
| --- | --- |
| `401` | Disable that key for the process lifetime and try another healthy key |
| `402 API_KEY_BUDGET_EXCEEDED` | Disable that key and try another healthy key |
| `402 NO_MORE_CREDITS` or `TEAM_BUDGET_EXCEEDED` | Mark the account/team unavailable and fail without sweeping the pool |
| `429` | Cool down that key using `Retry-After` when available, then try another healthy key |
| Other `4xx` | Return the error; do not rotate |
| `5xx` | Return the provider error; do not replay across keys |
| Network failure or timeout | Return an ambiguous-request error; do not replay with another key |
| Malformed or oversized response | Fail closed; do not replay |

When every healthy key is cooling down, the MCP error reports an approximate
retry delay without exposing credentials. Key diagnostics use short SHA-256
fingerprints only.

This table describes the Exa gateway's own key-selection behavior. Hermes may
reconnect and invoke an MCP tool once more after a transport failure. PV Wiki
intentionally keeps no action ledger for that delegated behavior, so a search
or fetch can be billed twice; completion inserts and Wiki.js upserts remain
idempotent. A deployment that requires strict at-most-once paid calls must pin
or patch Hermes to honor `idempotentHint=false` before activation.

Rotate keys by atomically replacing the file, preserving ownership and mode
`0600`, then restarting only the Exa MCP process. The pool is process-local;
a restart reloads the key list and clears its cursor, cooldowns, and disabled
state. Do not restart the PV Wiki MCP or delete completion state merely to
rotate Exa credentials.

The gateway still calls Exa's cloud `/search` and `/contents` endpoints. Keep
provider rate limits and team budgets in force across all keys. Do not treat
key rotation as additional aggregate quota.

## Monitoring and incident handling

Call `pv_research_status` to obtain:

- `completed`;
- `publishable`;
- `published`;
- `pending_publication`;
- whether an unresearched catalogue product is currently available.

The status tool does not perform a paid Exa request. Alert on systemic
failures such as an unavailable state database, failed catalogue refresh,
invalid secret file, every key unavailable, or repeated Wiki.js publication
failure. Treat `no_datasheet`, `ambiguous`, `insufficient_identity`, and
`out_of_scope` as normal completed outcomes, not incidents.

Use Hermes cron history for research-session diagnostics and service-manager
logs for MCP startup/transport failures. Redact raw credentials, database URLs,
catalogue payloads, full extracted bodies, and Wiki.js tokens before retaining
or sharing logs.

Do not use the legacy worker's `/status`, queue due counts, lease expiry,
credit counters, circuits, or n8n execution history to judge the Hermes path.

## Backups and upgrades

Back up:

- the dedicated PV Wiki state database, including
  `product_research_completions` and the catalogue snapshot;
- Hermes configuration, installed runtime skill, and cron definition;
- MCP service definitions and the deployed commit manifest;
- the Wiki.js application database through Wiki.js's own backup procedure;
- secret metadata and recovery procedure, but not raw keys in general-purpose
  archives.

Test restoration. The completion table is what prevents finished products
from being researched again, so a state rollback may deliberately forget
newer completions.

Before an upgrade:

1. Disable the Hermes cron.
2. Inspect and, when safe, publish pending completions.
3. Back up state and configuration.
4. Install one pinned revision in a new virtual environment.
5. Start all three MCP servers and validate their isolated tool lists.
6. Call `pv_research_status` and compare counts with the backup.
7. Test one private/unpublished page or a publication-only retry.
8. Re-enable the serialized cron only after review.

Do not automatically delete or rewrite published Wiki.js pages during an
upgrade. Human-authored text outside the managed block may have changed.

## Legacy rollback

Treat `deploy/n8n` as rollback-only. Before reactivating it:

1. Disable the Hermes cron and confirm no session is running.
2. Stop the three MCP processes if their service manager could restart them.
3. Back up the current completion and legacy state tables.
4. Restore the exact compatible worker code, n8n workflows, state schema, and
   credentials.
5. Keep the legacy workflows inactive until one supervised private-page test
   succeeds.
6. Activate only the legacy schedule.

The legacy worker retains its bounded rounds, queries, extracts, credits,
deadline, global budgets, leases, retries, provider circuits, and action
ledger. Those controls belong only to that rollback implementation. Preserve
`product_research_completions` during rollback so a later return to Hermes can
still exclude all known completed products.
