# Product source contract

The supported source is PostgreSQL table `public.products`. The runtime issues
only `SELECT` statements and explicitly starts a read-only transaction.

| Column | Type | Meaning |
| --- | --- | --- |
| `product_id` | varchar, non-null | Stable internal key and idempotency key |
| `brand_code` | varchar, nullable | Internal brand hint; not always a public manufacturer name |
| `family_code` | varchar, nullable | Optional family hint |
| `product_name` | text, nullable | Best available public model/name search seed |
| `unit_of_measure` | varchar, nullable | Catalogue metadata |
| `created_at` | timestamp | Source creation time |
| `updated_at` | timestamp | Incremental refresh watermark |

Known data characteristics at design time: about 1,500 unique product IDs;
brand and family values can be blank; a small number of names are blank; names
can repeat or be generic. Therefore never use `product_name` as an identity key
and never assume `brand_code` names the manufacturer.

Required connection variables are standard libpq variables: `PGHOST`,
`PGPORT`, `PGDATABASE`, `PGUSER`, `PGPASSWORD`, and `PGSSLMODE`.
`PGSSLMODE` is normalized before connecting and must be one of the standard
libpq modes: `disable`, `allow`, `prefer`, `require`, `verify-ca`, or
`verify-full`; blank and unknown values are refused. Use `verify-full`
whenever the certificate chain and hostname can be validated. `disable`,
`allow`, and `prefer` may use an unencrypted connection and are supported only
for a catalogue server without TLS after the operator accepts that network
risk. For containerized `verify-ca` or `verify-full`, set `PGSSLROOTCERT` to
the read-only mounted CA path. The deployment bundle mounts the host path from
`CATALOGUE_CA_PATH` at `/run/pv-wiki/catalogue-ca.pem`; non-verifying modes
ignore it.

The configured account must have SELECT-only privileges. Non-live `doctor`
reports a missing `PGSSLMODE` with the other required variables. `doctor
--live`, `sync-db`, and the runtime's environment-based connection factory
apply the same explicit-mode check before opening PostgreSQL. The live doctor
reports the selected mode and warns when the connection may be unencrypted. It
also forces the probe transaction read-only and fails if that transaction or
the fixed product query cannot run; review the PostgreSQL role grants
separately.

External search sends only `product_name` by default. `family_code` always
remains local and never becomes a public category or search term. `brand_code`
is included only when the operator explicitly enables
`PV_WIKI_TAVILY_INCLUDE_INTERNAL_HINTS`. `product_id` is never a Tavily or AI
input.
