# PV wiki

PV wiki is a Hermes skill and a small Python runtime for continuously turning a
read-only PostgreSQL product catalogue into cited Wiki.js pages. Each scheduled
run handles one bounded product: discover candidates with Tavily, let the agent
judge identity and evidence, then create or update only the managed section of
the corresponding Wiki.js page.

The first release deliberately favors correctness and low operating cost over
speed. Work is leased and persisted in SQLite, so interrupted runs are safe to
retry and a catalogue of roughly 1,500 products can be processed gradually.

## What it does

- Reads `public.products` through a PostgreSQL read-only account.
- Keeps a durable queue and audit trail in a local SQLite database.
- Uses Tavily Search and Extract with explicit per-product limits.
- Requires a structured, cited AI decision before publishing.
- Upserts Wiki.js 2.5 pages by `locale + path` and preserves human notes.
- Creates private drafts by default and preserves existing page visibility.
- Rechecks successful pages periodically and backs off unresolved products.
- Documents an explicit Hermes cron job with a four-hour schedule.

It does not deploy Wiki.js, copy full copyrighted datasheets, write to the
product database, or silently publish low-confidence matches.

Wiki.js owns and connects to its own database as part of the Wiki.js
deployment. PV wiki never provisions that database and never asks Wiki.js to
reuse the read-only product catalogue; it writes pages only through the
configured Wiki.js GraphQL API.

## Layout

```text
skills/wikijs-sync-products/
  SKILL.md                 Hermes instructions and bounded workflow
  scripts/                 portable Python runtime
  references/              setup, policies, and decision contract
  templates/               managed Wiki.js page shape
tests/                     offline unit tests
```

## Quick start

1. Create a dedicated Wiki.js API key limited to the configured product path.
2. Copy `.env.example` to the ignored `.env`, fill the values, and export them
   in the shell that runs the CLI (`set -a; . ./.env; set +a` in Bash).
   Hermes itself stores declared secrets in `~/.hermes/.env`. Keep the required
   `PGSSLMODE=verify-full` when certificate and hostname verification works;
   `require` is accepted for an understood internal-certificate limitation,
   while weaker modes are rejected.
3. Install the runtime with `python3 -m pip install -e .`.
4. Run `pv-wiki doctor --live`, then `pv-wiki sync-db`.
   Run `pv-wiki publish-home` once to create the Wiki.js landing page, and set
   that configured path as the Wiki.js home page.
5. Add this repository as a private Hermes tap, install the skill, and invoke
   it manually once. A private tap requires `GITHUB_TOKEN` in Hermes's `.env`.

   ```bash
   hermes skills tap add sulirang/pv-wiki
   hermes skills install sulirang/pv-wiki/wikijs-sync-products
   ```
6. After reviewing that run, create a slow scheduled job:

   ```bash
   hermes cron create "every 4h" \
     "Run exactly one bounded PV wiki maintenance cycle, then stop." \
     --skill wikijs-sync-products \
     --name "PV wiki maintenance"
   ```

The skill's [Wiki.js setup guide](skills/wikijs-sync-products/references/wikijs-setup.md)
and [operations guide](skills/wikijs-sync-products/references/operations.md)
contain the production checklist. Keep the SQLite state file on persistent
storage if Hermes runs in a container.

## Safe rollout

Start against a Wiki.js staging path with 20–50 representative products. Review
the generated identity matches, citations, page paths, and preserved human
content before enabling unattended publishing. The recommended schedule
processes at most six products per day. New pages default to private and
unpublished; change `WIKIJS_NEW_PAGE_PRIVATE` and
`WIKIJS_NEW_PAGE_PUBLISHED` only after confirming the intended access boundary.

## Development

```bash
python3 -m unittest discover -s tests -v
python3 -m unittest discover -s skills/wikijs-sync-products/tests -v
```

All tests are offline and use mocked HTTP/temporary databases.
