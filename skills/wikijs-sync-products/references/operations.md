# Operations

## Cadence and budget

The recommended cron job runs every four hours and processes one product,
roughly six per day. At that rate an initial 1,500-product catalogue takes about
eight months, which is intentional. Use at most three basic Tavily searches and
five extracted URLs per product. Track returned usage; set an external monthly
credit alert (suggested initial cap: 800 credits).

Do not enable Tavily Research by default. Escalate manually only for important
products with genuine source conflicts.

Extracted text written to the terminal is capped per source (30,000 characters
by default) so cron logs do not retain entire documents. The runtime records a
SHA-256 when truncation occurs; keep only citations and compact evidence.

## PostgreSQL transport security

`PGSSLMODE` is mandatory. Prefer `verify-full`, with the appropriate trusted
CA configured for libpq, so both the certificate chain and server hostname are
checked. `verify-ca` is accepted when hostname verification is not available.
For an accepted internal/self-signed certificate limitation, `require` may be
used to guarantee encryption, but it does not authenticate the server. The
runtime refuses `disable`, `allow`, `prefer`, blank, and unknown modes before a
connection is opened.

Run `pv-wiki doctor` to inspect required configuration without network probes,
then `pv-wiki doctor --live` to exercise the protected PostgreSQL connection.

## Wiki.js visibility

New pages default to private drafts. Keep `WIKIJS_NEW_PAGE_PRIVATE=true` and
`WIKIJS_NEW_PAGE_PUBLISHED=false` during staging. After confirming Wiki.js
authentication, group permissions, and the intended path boundary, explicitly
change those settings if unattended runs should publish new pages. Updates to
existing pages preserve the visibility returned by Wiki.js.

## State and retries

The SQLite file defaults to `~/.hermes/data/pv-wiki/state.sqlite3`. Back it up
and mount its directory persistently. Claims have finite leases; expired claims
are audited and enter short transient backoff. Database field changes reset a
product to pending.

Each attempt persistently budgets one Search batch (at most three Tavily
queries) and one Extract batch (at most five URLs). Extract URLs must be members
of the recorded Search candidates, and publish citations must be members of the
completed Extract set. A process crash conservatively consumes that lease's
web budget; a later lease can retry after backoff.

Extract uses Tavily's advanced depth with up to five relevance-ranked chunks
per source so PDF tables and other structured specifications are retained more
reliably. This costs more than basic extraction; keep the five-URL cap and
prefer one exact official datasheet over several mirrors.

- no datasheet: retry after approximately 30, then 90, then 180 days;
- ambiguous or insufficient identity: retry after 30 days;
- transient error: retry after approximately 1, then 6, then 24 hours;
- detected Wiki.js edit conflict: retry after about 24 hours;
- published: recheck after 365 days. The deliberately slow six-per-day cadence
  cannot provide a 90-day refresh for a catalogue of this size.

Inspect progress with `pv-wiki status`. Audit payloads contain metadata and
source URLs, not credentials or full downloaded documents.

## Rollout

1. Dry-run 20–50 varied products and review decisions locally.
2. Publish into a staging Wiki.js prefix and test human-edit preservation.
3. Enable automatic publication only for high-confidence official sources.
4. Add mirrors, larger batches, PDF/OCR, or multilingual pages only after an
   evaluated sample demonstrates acceptable precision.

Rotate API/database credentials after suspected exposure. Keep Wiki.js and
Tavily keys separate from Hermes logs and shell history.
