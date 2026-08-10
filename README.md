# PV Wiki

PV Wiki turns a read-only PostgreSQL product catalogue into cited Wiki.js
pages. Since version 0.4, the default runtime is a Hermes cron job with two
small MCP servers:

```text
Hermes cron (one fresh, serialized research session)
  ├── mcp-exa-pool
  │     ├── web_search_exa
  │     ├── web_search_advanced_exa
  │     └── web_fetch_exa
  └── mcp-pv-wiki
        ├── pv_pending_publication
        ├── pv_next_product
        ├── pv_save_research
        ├── pv_publish_result
        └── pv_research_status
                 │
                 ├── read-only product catalogue
                 ├── product_research_completions
                 └── Wiki.js GraphQL API
```

Hermes owns the research plan and recurring schedule. The Exa MCP gateway
owns credential selection and read-only Exa calls. PV Wiki owns only catalogue
selection, durable completion, evidence validation, and publication.

`web_fetch_exa` returns each successful extraction as JSON containing its
normalized public `url`, exact `content`, content SHA-256, and a versioned HMAC
`receipt`. `pv_save_research` requires the unchanged URL/content/receipt tuple
and verifies it before the existing catalogue-identity, source-authority, and
fact-grounding gates. Search snippets are never signed and therefore cannot be
submitted as extracted evidence. Receipts are stateless and add no research
attempt, lease, retry, budget, timestamp, or action-ledger state.

## Responsibility boundaries

| Component | Responsibility |
| --- | --- |
| Hermes cron | Run one autonomous research session at a time and decide when enough evidence exists |
| `pv-wiki-exa-mcp` | Expose the three Exa-compatible read-only tools through a health-aware API-key pool |
| `pv-wiki-research-mcp` | Refresh the catalogue, select an unfinished product, save its first completion, and publish completed results |
| Completion store | Permanently exclude completed `product_id` values and retain pending publication state |
| Publisher | Apply an already-completed publish decision to Wiki.js without invoking research |

The self-hosted component is the MCP gateway, not the Exa search backend.
Search and content extraction still use Exa's cloud API. Use only keys owned by
the authorized Exa account or team, and keep Exa's service-level limits and
account budgets enabled. Key rotation is for availability and isolation, not
for bypassing provider limits.

## Completion and publication contract

`product_research_completions.product_id` is the durable idempotency key. Both
publish and non-publish outcomes create a completion. Once present, that
product is excluded by `pv_next_product` even if its catalogue row changes
later. Re-research requires an explicit, operator-approved deletion of the
completion row; it never happens automatically.

Selection also uses a process-local wraparound cursor over unfinished product
ids. This lets the catalogue continue when one product has no safely provable
public outcome, without persisting a lease, backoff, or retry schedule. Such a
product remains unfinished and can be reconsidered after wraparound; completed
products remain excluded by the database primary key.

The save path checks the `source_hash` returned by `pv_next_product` before it
inserts the completion. This prevents a stale Hermes session from completing a
newer catalogue snapshot. The insert is first-writer-wins and never overwrites
an existing completion.

The same save boundary preserves the legacy publication safety policy without
its scheduler limits: a publish result must match the catalogue manufacturer
alias and exact model, establish an authoritative primary datasheet (or the
explicitly enabled two-mirror fallback), and ground every fact's model, name,
value, and unit in an exact quote from that primary evidence. A table fact may
also provide an exact `model_quote` header row. Permanent non-publish outcomes
must submit extracted evidence plus at least one exact `conclusion_evidence`
quote; `no_datasheet` is not accepted as an unsupported assertion.

Publication is deliberately separate:

1. `pv_save_research` validates and stores the completed decision.
2. A publishable completion becomes pending publication.
3. `pv_publish_result` or `pv-wiki-publish-researched` applies it to Wiki.js.
4. A Wiki.js failure leaves it pending. The next run retries publication only;
   it does not research the product again.

Opening the completion store also backfills products successfully published by
the legacy worker. A legacy `products.last_success_at` row with no completion
is inserted as already completed and published; `ON CONFLICT DO NOTHING`
remains the concurrent-initialization safeguard. An upgrade therefore does not
research previously finished products again.

## Research policy

The new Hermes path intentionally has no PV Wiki research scheduler or
per-product research budget. The legacy limits of three rounds, seven queries,
five extracted URLs, 20 credits, and 600 seconds do not apply. Neither do the
legacy global daily/monthly credit reservations, product leases, retry queue,
or uncertain-action ledger.

Hermes decides how to research. Its recurring PV Wiki job must expose only:

- `mcp-exa-pool`, restricted to `web_search_exa`,
  `web_search_advanced_exa`, and `web_fetch_exa`;
- `mcp-pv-wiki`, restricted to the five `pv_*` tools shown above.

Do not enable Hermes native web, browser, terminal, delegation, or an Exa
`agent_run` tool for the recurring job. This keeps every public-web request on
the self-hosted Exa gateway and avoids nesting another research agent inside
Hermes.

The key pool still enforces provider health semantics. It rotates after every
successful request, disables invalid or key-budget-exhausted credentials,
cools down rate-limited credentials, and stops on team/account budget errors.
The Exa gateway itself does not replay an ambiguous network or provider
request across keys. Hermes currently may reconnect and invoke an MCP tool once
more after a transport failure; this project deliberately adds no local action
ledger for that delegated behavior. Search/fetch may therefore consume a
duplicate provider call, while completion inserts and Wiki.js upserts remain
idempotent.

## Install and configure

Python 3.11 or newer is required. Install the package and MCP Python SDK v2:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install .
```

Follow [`deploy/hermes/README.md`](deploy/hermes/README.md) for the production
layout and use its
[`mcp-config.yaml.example`](deploy/hermes/mcp-config.yaml.example) as the
Hermes MCP configuration template. The recurring research procedure is in
[`skills/pv-wiki-research/SKILL.md`](skills/pv-wiki-research/SKILL.md).

For production, place the Exa keys in one mode-`0600` secret file. Also create
a separate mode-`0600` receipt HMAC key file containing at least 32 random
bytes. Never reuse an Exa key as the receipt key:

```dotenv
EXA_API_KEYS_FILE=/run/secrets/pv-wiki-exa-keys
PV_WIKI_EVIDENCE_HMAC_KEY_FILE=/run/secrets/pv-wiki-evidence-hmac-key
```

Only file paths belong in MCP process configuration. Both stdio MCP processes
receive the receipt-key path; only the Exa MCP receives the Exa-key path. Raw
keys must
not appear in Hermes configuration, prompts, skill files, command-line
arguments, Git, or logs. `EXA_API_KEYS` and `EXA_API_KEY` remain code-level
compatibility fallbacks for legacy migration, but the supported production
deployment uses `EXA_API_KEYS_FILE`.

Configure the research-state MCP with:

- `PV_WIKI_STATE_DATABASE_URL` for its dedicated production PostgreSQL state
  database (`PV_WIKI_STATE_PATH` is a local/test compatibility option);
- `PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER`, `PGPASSWORD`, and an explicit
  `PGSSLMODE` for the read-only catalogue;
- the restricted `WIKIJS_URL` and `WIKIJS_TOKEN`, plus the desired Wiki.js
  path, locale, and new-page visibility settings.

The bundled supplier registry supplies approved manufacturer aliases and
domains for known brands. For deployment-specific brands, configure
`PV_WIKI_PUBLIC_BRAND_ALIASES_JSON` together with
`PV_WIKI_TRUSTED_SOURCE_DOMAINS_JSON` in the PV Wiki MCP only. Optional
publication thresholds remain `PV_WIKI_AUTO_PUBLISH_MIN_CONFIDENCE` and
`PV_WIKI_MIN_FACT_CONFIDENCE`; the two-mirror fallback stays disabled unless
an operator explicitly sets `PV_WIKI_ALLOW_MIRRORS=true`. A model-discovered
manufacturer or self-declared source type cannot authorize publication:
publish decisions fail closed without an operator-approved catalogue brand
identity and trusted source domain (or the explicitly enabled two-mirror
policy).

The Hermes model is configured in Hermes itself. The research-state MCP does
not need Exa or AI credentials, and the Exa MCP does not need catalogue,
state-database, or Wiki.js credentials.

Both MCP servers use stdio by default:

```bash
pv-wiki-exa-mcp
pv-wiki-research-mcp
```

Streamable HTTP is opt-in for separated deployments. It binds to loopback by
default; do not publish either MCP endpoint directly to the Internet. See the
deployment guide for the explicit transport, host, port, and private-network
requirements.

## Hermes cron sequence

Keep exactly one non-overlapping recurring PV Wiki job. Each fresh session
must:

1. Call `pv_pending_publication`. Publish the pending result before starting
   new research. If one is found, call `pv_publish_result` and end this run.
2. Otherwise call `pv_next_product`. Its default `refresh_catalogue=true` performs the
   read-only catalogue refresh before the completion anti-join.
3. Stop cleanly when it returns `found=false`.
4. Research the returned product using only the three Exa MCP tools.
5. Submit decision schema version `2`, the returned `product_id` and
   `source_hash`, and each unchanged `url`/`content`/`receipt` tuple returned by
   `web_fetch_exa` as the extracted evidence documents to
   `pv_save_research`.
6. If the stored completion is publishable, call `pv_publish_result` for that
   `product_id`, then end the run.

The selector is an anti-join, not a lease. Serialization prevents two fresh
sessions from doing the same unfinished research concurrently; the completion
primary key prevents a completed product from being accepted twice.

## Operations

Use `pv_research_status` for completion and pending-publication counts. The
publisher can also run independently of Hermes:

```bash
pv-wiki-publish-researched
pv-wiki-publish-researched --product-id PRODUCT_ID
```

The first form publishes the oldest pending completion. Both forms print one
JSON result and never call Exa or an AI model. Detailed key rotation, recovery,
backup, and rollback procedures are in
[`references/operations.md`](skills/wikijs-sync-products/references/operations.md).
The completion schema and migration behavior are in
[`references/database-schema.md`](skills/wikijs-sync-products/references/database-schema.md).

## Legacy n8n rollback path

The version 0.3 n8n workflows and bounded worker remain under
[`deploy/n8n`](deploy/n8n/README.md) only as a rollback/migration path. They are
not the version 0.4 default. Do not run the legacy n8n product workflow and the
Hermes cron at the same time.

The legacy 3/7/5/20/600 limits, global budgets, leases, queue retries,
provider circuits, and action ledger remain implemented for that rollback
worker only. No legacy code or audit tables are deleted by the Hermes upgrade.

## Development

The test suites are offline and use mocked providers and temporary state:

```bash
python -m pip install -e '.[dev]'
python -m unittest discover -s tests -v
python -m unittest discover -s skills/wikijs-sync-products/tests -v
```

CI runs both suites on Python 3.11, 3.12, and 3.13.
