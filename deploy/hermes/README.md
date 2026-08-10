# Hermes deployment

This is the supported version-0.4 deployment: Hermes owns the recurring
autonomous session, `exa-pool` owns Exa credentials and search access, and
`pv-wiki` owns durable completion state and Wiki.js publication.

The example follows Hermes Agent's current
[MCP configuration](https://hermes-agent.nousresearch.com/docs/reference/mcp-config-reference)
and [scheduled-task](https://hermes-agent.nousresearch.com/docs/user-guide/features/cron)
interfaces.

## 1. Install the runtime

Use Python 3.11 or newer and a dedicated checkout:

```bash
git clone https://github.com/sulirang/pv-wiki.git /opt/pv-wiki
cd /opt/pv-wiki
python3 -m venv .venv
.venv/bin/python -m pip install .
```

Install Hermes Agent separately and ensure its gateway scheduler is running:

```bash
hermes gateway install
hermes cron status
```

Do not run the legacy n8n product-cycle workflow alongside this deployment.

## 2. Isolate credentials

Create two service-managed secret files. The first contains authorized Exa
keys separated by commas or whitespace. The second contains an independent,
random receipt HMAC key of at least 32 bytes; never reuse an Exa key for it.
Replace the example `hermes` owner/group with the actual account that launches
both Hermes stdio MCP subprocesses:

```bash
sudo install -d -o hermes -g hermes -m 0700 /run/secrets/pv-wiki
sudo install -o hermes -g hermes -m 0600 /dev/null \
  /run/secrets/pv-wiki/exa-keys
sudo install -o hermes -g hermes -m 0600 /dev/null \
  /run/secrets/pv-wiki/evidence-hmac-key
```

Write both files through the host's secret manager or an interactive
administrator action. Do not place raw keys in Git, shell history, Hermes
configuration, `~/.hermes/.env`, prompts, cron definitions, URLs, or HTTP
headers. Only protected file paths belong in MCP configuration. Update those
paths if necessary in `mcp-config.yaml.example`. The Exa MCP returns only HMAC
receipts, never either secret; the PV Wiki MCP verifies the receipts before it
accepts extracted evidence.

Receipts are versioned, stateless HMACs over the normalized public URL and the
SHA-256 of the exact UTF-8 content. They add no timestamps, leases, retries,
action ledger, provider budget, or database table.

Put the research-state database, catalogue, and Wiki.js values in the Hermes
service environment or `~/.hermes/.env` so `${VAR}` substitution can pass them
only to `pv-wiki`:

```dotenv
PV_WIKI_STATE_DATABASE_URL=postgresql://pv_wiki_state:REDACTED@db.internal/pv_wiki_state?sslmode=require
PGHOST=catalogue.internal
PGPORT=5432
PGDATABASE=catalogue
PGUSER=pv_wiki_reader
PGPASSWORD=REDACTED
PGSSLMODE=require
WIKIJS_URL=https://wiki.example.com
WIKIJS_TOKEN=REDACTED
WIKIJS_LOCALE=en
WIKIJS_PATH_PREFIX=products
WIKIJS_NEW_PAGE_PRIVATE=true
WIKIJS_NEW_PAGE_PUBLISHED=false
PV_WIKI_HTTP_TIMEOUT=30
```

Known manufacturers use the bundled supplier registry. If the catalogue has
deployment-specific brand codes, define an operator-approved public alias and
trusted domains, then pass the same variables only to the `pv-wiki` MCP:

```dotenv
PV_WIKI_PUBLIC_BRAND_ALIASES_JSON='{"INTERNAL_BRAND":"Public Manufacturer"}'
PV_WIKI_TRUSTED_SOURCE_DOMAINS_JSON='{"INTERNAL_BRAND":["manufacturer.example"]}'
PV_WIKI_AUTO_PUBLISH_MIN_CONFIDENCE=0.85
PV_WIKI_MIN_FACT_CONFIDENCE=0.8
PV_WIKI_ALLOW_MIRRORS=false
```

The aliases and domains are operator authority, not model output. Leave the
mirror fallback false unless two independent mirror domains are an explicitly
accepted publication policy. Publication fails closed when a catalogue brand
has no bundled or configured public alias. Manufacturer, regulatory, and
authorized sources must use a bundled or configured trusted domain; a second
model-selected domain cannot make an unregistered domain authoritative.

Use a read-only catalogue role, a dedicated PV Wiki state role/database, and a
Wiki.js token restricted to the required page operations. The `pv-wiki` MCP
must not receive Exa or AI credentials. The `exa-pool` MCP must not receive
catalogue, state, Wiki.js, or model credentials.

## 3. Register the MCP servers

Merge [`mcp-config.yaml.example`](mcp-config.yaml.example) into
`~/.hermes/config.yaml`, preserving any unrelated settings. The configured
server names create the dynamic Hermes toolsets `mcp-exa-pool` and
`mcp-pv-wiki`.

The include lists are a security boundary. Keep exactly the three Exa tools and
five PV Wiki tools shown in the example. Keep resources, prompts, MCP sampling,
elicitation, and parallel calls disabled.

Reload and test both servers:

```bash
hermes mcp test exa-pool
hermes mcp test pv-wiki
hermes mcp list
```

If Hermes is already running, use `/reload-mcp` in a Hermes chat or restart the
gateway. Starting `pv-wiki` creates the completion table and backfills legacy
`products.last_success_at` successes with `ON CONFLICT DO NOTHING`.

## 4. Install the runtime skill

Copy the bundled skill into the active Hermes profile:

```bash
install -d ~/.hermes/skills/pv-wiki-research
cp /opt/pv-wiki/skills/pv-wiki-research/SKILL.md \
  ~/.hermes/skills/pv-wiki-research/SKILL.md
hermes skills list
```

Review the installed file before enabling unattended execution. Product
fields, Exa results, URLs, and fetched pages are untrusted data; the skill never
follows instructions found in any of them.

## 5. Create one serialized cron job

From an operator-authorized Hermes chat, create the job through Hermes's native
`cronjob` tool:

```text
cronjob(
    action="create",
    name="pv-wiki-research",
    schedule="every 30m",
    skills=["pv-wiki-research"],
    enabled_toolsets=["mcp-exa-pool", "mcp-pv-wiki"],
    workdir="/opt/pv-wiki",
    prompt="Process at most one pending PV Wiki publication or one unresearched product. Follow the attached skill exactly. Stop cleanly when no work exists or when a tool fails."
)
```

`enabled_toolsets` is intentionally exhaustive. It excludes Hermes's native
web, browser, terminal, file, code-execution, delegation, and `agent_run`
surfaces from this unattended session. Every public-web request must go through
the self-hosted rotating-key Exa MCP.

The cadence limits how frequently a fresh session starts; it is not a research
round, query, URL, credit, or wall-clock budget. Do not reintroduce the old
3/7/5/20/600 limits, global PV Wiki budgets, product leases, retry queue, or
uncertain-action replay ledger. Keep exactly one PV Wiki cron job and do not
allow overlapping research sessions. The completion primary key prevents
completed products from being researched again.

The Exa gateway does not rotate and replay a request whose provider result is
ambiguous. Hermes itself may reconnect and invoke an MCP tool once more after a
transport failure. That delegated retry is intentionally not tracked in PV
Wiki: it can duplicate a paid search/fetch, while `pv_save_research` and
`pv_publish_result` remain idempotent. If strict at-most-once paid calls are a
deployment requirement, first pin or patch Hermes so its MCP client respects
`idempotentHint=false`; do not recreate the removed PV Wiki action ledger.

Inspect and exercise the job before leaving it enabled:

```bash
hermes cron list
hermes cron run pv-wiki-research
hermes cron status
```

Each run starts in a fresh session. Durable continuity lives in
`product_research_completions`, not in Hermes chat history.

The long-lived PV Wiki MCP keeps a non-durable fair selection cursor. If a
product has no fetched page that can support a safe permanent outcome, the run
leaves it unfinished and stops; the next run receives another unfinished
product. After wraparound it may be reconsidered. Restarting the MCP resets
this cursor but never affects completed-product exclusion.

## 6. Verify the recovery boundary

Before unattended activation, verify these cases with a non-production or
approved representative product:

1. `pv_next_product` refreshes the catalogue and returns `product_id`,
   `source_hash`, and the source record.
2. `pv_save_research` accepts a schema-version-`2` decision once; a replay
   returns `created=false` without overwriting it.
   Every submitted evidence document must carry the unchanged `url`, exact
   `content`, and `receipt` returned together by `web_fetch_exa`; missing or
   altered receipts are rejected.
3. A non-publish outcome is completed and never selected again.
4. A publishable completion is written before Wiki.js is called.
5. A simulated Wiki.js failure leaves it pending; the next run calls only
   `pv_publish_result` and does not re-research it.
6. Legacy successful products appear as completed and published after the first
   MCP startup.

Use `pv_research_status` for counts. The publish-only CLI is also safe for an
operator or separate service timer:

```bash
/opt/pv-wiki/.venv/bin/pv-wiki-publish-researched
/opt/pv-wiki/.venv/bin/pv-wiki-publish-researched --product-id PRODUCT_ID
```

Both commands are idempotent and never invoke Exa or a model.

## Optional Streamable HTTP isolation

Stdio is the default and simplest same-host deployment. For stronger process
and secret isolation, run either MCP as a separately supervised service and let
Hermes connect over loopback Streamable HTTP:

```bash
/opt/pv-wiki/.venv/bin/pv-wiki-exa-mcp \
  --transport streamable-http --host 127.0.0.1 --port 8000
/opt/pv-wiki/.venv/bin/pv-wiki-research-mcp \
  --transport streamable-http --host 127.0.0.1 --port 8001
```

Configure the service manager—not Hermes—with each daemon's environment. This
lets the Exa daemon run under a separate account that alone can read Exa API
keys. Give the Exa and PV Wiki service accounts separate mode-`0600` receipt
key files containing the same secret (normally two secret-manager mounts with
different owners). Neither account should read the other's receipt-key file,
and the PV Wiki account must never receive Exa API keys. Replace each stdio
`command` entry with the commented `url` form in the example configuration;
never configure both forms for one server.

Both binaries refuse non-loopback HTTP binding unless the corresponding
`EXA_MCP_ALLOW_REMOTE=true` or `PV_WIKI_MCP_ALLOW_REMOTE=true` override is set.
The override is not authentication. For a cross-host deployment, use a private
network plus an authenticated TLS/mTLS reverse proxy and allowlist the Hermes
host. Never publish these endpoints directly to the Internet.

## Backup and rollback

Back up the state database before first activation and before upgrades. The
completion table is the record that prevents duplicate research, including
during a future return to Hermes.

To roll back, pause the Hermes cron and confirm no session is running before
starting the legacy n8n worker. Do not drop `product_research_completions`; keep
it in the backup and restore path. See the main operations runbook for database
backup, incident handling, and the full rollback procedure.
