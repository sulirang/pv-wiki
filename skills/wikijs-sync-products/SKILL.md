---
name: wikijs-sync-products
description: Install, upgrade, validate, repair, roll back, or remove the Hermes-scheduled PV Wiki product-research deployment on a user-authorized host. Use when configuring the rotating-key Exa MCP gateway, the PV Wiki research-state MCP, Hermes MCP tool allowlists and cron, the durable completion database, read-only catalogue access, or restricted Wiki.js publication; treat the bundled n8n worker as a legacy rollback path only.
---

# Install the Hermes PV Wiki automation

Deploy Hermes as the recurring research owner. Deploy the rotating-key Exa MCP
as the only public-web boundary and the PV Wiki MCP as the catalogue,
completion, and publication boundary. Do not deploy n8n for a new installation.

Read these resources before changing a host:

- [`deploy/hermes/README.md`](../../deploy/hermes/README.md) for the production
  layout and Hermes cron configuration;
- [`deploy/hermes/mcp-config.yaml.example`](../../deploy/hermes/mcp-config.yaml.example)
  for the two MCP server definitions and tool allowlists;
- [`skills/pv-wiki-research/SKILL.md`](../pv-wiki-research/SKILL.md) for the
  recurring research procedure;
- [database boundaries](references/database-schema.md);
- [operations and rollback](references/operations.md);
- [Wiki.js setup](references/wikijs-setup.md).

## Preserve the security boundaries

- Work only on the exact host, repository, databases, and Wiki.js prefix the
  user authorized. Perform read-only discovery before mutation.
- Keep the product catalogue account SELECT-only and require an explicit
  `PGSSLMODE`. Prefer `verify-full` with a mounted CA.
- Keep the PV Wiki state database, product catalogue, and Wiki.js application
  database logically separate. Write Wiki.js only through its restricted API
  token.
- Put Exa credentials only in the mode-`0600` file named by
  `EXA_API_KEYS_FILE`. Create a separate mode-`0600`, at-least-32-byte random
  key named by `PV_WIKI_EVIDENCE_HMAC_KEY_FILE`; never reuse an Exa key. Put
  only paths in MCP configuration. Never put raw keys in Hermes configuration,
  prompts, skill files, command-line arguments, Git, or logs.
- Treat the self-hosted Exa component as an MCP gateway. State clearly that
  search and extraction still use Exa's cloud API.
- Keep stdio as the default transport on the same host. If a split deployment
  requires Streamable HTTP, bind to loopback or an authenticated private
  network. Never expose either MCP endpoint publicly.
- Do not enable Hermes native web, browser, terminal, delegation, or Exa
  `agent_run` in the recurring job. Enable only `mcp-exa-pool` and
  `mcp-pv-wiki` with their explicit tool allowlists.
- Keep one serialized recurring job. Do not run the legacy n8n product cycle
  concurrently.
- Obtain explicit approval before activating a schedule, publishing public
  pages, deleting a completion, deleting state, removing volumes, or revoking
  external credentials.

## 1. Establish scope and discover read-only

Collect the authorized host and directory, Hermes installation and owner,
state-database location, read-only catalogue endpoint, Wiki.js URL and path
prefix, secret installation method, backup location, and desired cron cadence.
Do not ask the user to paste long-lived secrets into chat.

Inspect only the authorized host. Record:

- the exact PV Wiki commit and Python version;
- the Hermes version, configuration path, service owner, and existing jobs;
- whether an old PV Wiki n8n workflow or worker is active;
- PostgreSQL reachability, TLS mode, role ownership, and backup status;
- the current Wiki.js visibility policy and restricted token scope;
- any existing MCP server names that would conflict with `exa-pool` or
  `pv-wiki`.

Stop and ask when ownership, the deployment boundary, an existing schedule, or
the active Wiki.js prefix is ambiguous. Stop the legacy n8n schedule before
enabling Hermes, but preserve it inactive for rollback.

## 2. Install one pinned revision

Check out the exact approved commit in a dedicated service directory. Create a
Python 3.11+ virtual environment and install the project:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install .
```

Record the commit and resolved dependency versions. Do not install from a
moving branch in production. Install services under an unprivileged account
and grant it only the secret-file and database access it needs.

## 3. Install secrets and application configuration

Create the Exa key file outside the repository, make it readable only by the
Exa MCP service account, and set:

```dotenv
EXA_API_KEYS_FILE=/run/secrets/pv-wiki-exa-keys
PV_WIKI_EVIDENCE_HMAC_KEY_FILE=/run/secrets/pv-wiki-evidence-hmac-key
```

Allow commas or whitespace between keys. Use keys from the same authorized Exa
account or team. Use rotation for availability, not to evade provider limits.
The implementation retains `EXA_API_KEYS` and `EXA_API_KEY` only as legacy
compatibility inputs; do not use them in a new production deployment.

Configure the PV Wiki MCP separately:

```dotenv
PV_WIKI_STATE_DATABASE_URL=postgresql://...
PV_WIKI_EVIDENCE_HMAC_KEY_FILE=/run/secrets/pv-wiki-evidence-hmac-key
PGHOST=...
PGPORT=5432
PGDATABASE=...
PGUSER=...                 # SELECT-only catalogue role
PGPASSWORD=...
PGSSLMODE=verify-full
PGSSLROOTCERT=/run/secrets/catalogue-ca.pem
WIKIJS_URL=https://wiki.example.com
WIKIJS_TOKEN=...
```

Set the Wiki.js prefix, locale, timeout, and new-page visibility according to
the accepted deployment policy. Keep new pages private and unpublished during
acceptance. Do not install Exa keys in the PV Wiki MCP and do not install
catalogue, state, or Wiki.js credentials in the Exa MCP.

Review the bundled supplier registry. For every deployment-specific catalogue
brand that may publish, configure both an operator-approved public alias and
its trusted manufacturer/regulatory/authorized domains through
`PV_WIKI_PUBLIC_BRAND_ALIASES_JSON` and
`PV_WIKI_TRUSTED_SOURCE_DOMAINS_JSON`. Model output and independent unregistered
domains cannot establish publication authority.

For stdio, both MCP subprocesses run as the Hermes user and may read the same
owner-only receipt-key file. For separately supervised HTTP services, mount
the same receipt secret into two different mode-`0600` files, each owned by its
service account. The receipt is stateless; do not add a receipt table,
timestamp, lease, retry, action ledger, or budget.

Configure the Hermes model through Hermes. Do not add the legacy worker's
`AI_*`, research-round, query, URL, credit, wall-clock, global-budget, lease,
or action-ledger settings to the new path.

## 4. Configure the MCP servers

Use the bundled MCP example. Run both servers over stdio on the same host:

```text
pv-wiki-exa-mcp
pv-wiki-research-mcp
```

Allow exactly these Exa tools:

- `web_search_exa`
- `web_search_advanced_exa`
- `web_fetch_exa`

Allow exactly these PV Wiki tools:

- `pv_pending_publication`
- `pv_next_product`
- `pv_save_research`
- `pv_publish_result`
- `pv_research_status`

Do not enable MCP prompts or resources. Keep parallel tool calls disabled until
the deployment has demonstrated that its Hermes job is serialized.

Starting the PV Wiki MCP creates `product_research_completions` when needed and
backfills legacy `products.last_success_at` publications with `ON CONFLICT DO
NOTHING`. Inspect aggregate status after initialization and confirm that old
successful products count as completed and published.

## 5. Configure the recurring Hermes job

Install the bundled `pv-wiki-research` runtime skill in the Hermes skill path.
Create one Hermes-native cron job using only the `mcp-exa-pool` and
`mcp-pv-wiki` toolsets. Do not substitute a shell cron loop or an n8n workflow.

Require every session to follow this order:

1. Check `pv_pending_publication` and retry that publication first.
2. If a pending completion exists, call `pv_publish_result` and end the run.
3. Otherwise call `pv_next_product` with its default catalogue refresh.
4. Stop when no unfinished product exists.
5. Research through the Exa MCP only.
6. Save one schema-version-`2` decision with the returned `product_id`,
   `source_hash`, and extracted evidence documents.
7. Publish the saved result only when it is publishable, then end the run.

Do not add the legacy three-round, seven-query, five-URL, 20-credit, or
600-second limits. Do not add global PV Wiki credit reservations, leases,
queue retries, or uncertain-action replay state. Hermes owns the research
session; the completion primary key owns cross-session idempotency.

## 6. Validate before activation

Validate configuration without printing secrets. Confirm:

- both MCP servers start and list only the intended tools;
- `pv_research_status` returns completion counts;
- `pv_next_product` refreshes the read-only catalogue and returns a stable
  `product_id` and `source_hash`;
- one representative product can be researched through Exa and saved once;
- every saved document uses an unchanged `url`, exact `content`, and `receipt`
  from one `web_fetch_exa` result, while missing or altered receipts fail;
- a repeated save returns the existing completion rather than overwriting it;
- a publishable completion creates or updates only the managed Wiki.js block;
- a simulated Wiki.js failure leaves the completion pending and the next run
  retries publication without invoking research;
- the Wiki.js page remains private/unpublished during acceptance;
- no raw Exa key, catalogue password, state URL, or Wiki.js token appears in
  Hermes history or logs.

Show the user the acceptance result and obtain approval before enabling the
cron or changing new-page visibility.

## 7. Operate, upgrade, and roll back

Use `pv_research_status` for durable counts and
`pv-wiki-publish-researched` for a publication-only recovery. Rotate Exa keys
by atomically replacing the secret file, preserving mode `0600`, and restarting
only the Exa MCP process.

Before an upgrade, disable the Hermes cron, drain or inspect pending
publication, back up the state database and Hermes configuration, install one
pinned revision, start both MCP servers, validate status and one private page,
then re-enable the schedule.

For rollback, stop the Hermes cron and MCP processes first. Restore a compatible
state backup if required, then reactivate the preserved legacy n8n worker only
after verifying that no Hermes research job can run. Never run both schedulers
against the same catalogue and Wiki.js prefix. Do not automatically remove
`product_research_completions`; it prevents old successful products from being
researched again after returning to Hermes.
