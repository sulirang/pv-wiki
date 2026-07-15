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
`PGSSLMODE` is normalized before connecting and must be `require`, `verify-ca`,
or `verify-full`; `disable`, `allow`, `prefer`, blank, and unknown values are
refused. Use `verify-full` whenever the certificate chain and hostname can be
validated. `require` is allowed for an accepted internal-certificate
limitation, but provides encryption without server identity verification.

The configured account must have SELECT-only privileges. Non-live `doctor`
reports a missing `PGSSLMODE` with the other required variables. `doctor
--live`, `sync-db`, and the runtime's environment-based connection factory
apply the same TLS check before opening PostgreSQL. The live doctor also forces
the probe transaction read-only and fails if that transaction or the fixed
product query cannot run; review the PostgreSQL role grants separately.

External search sends only `product_name` by default. `brand_code` and
`family_code` remain local hints unless the operator explicitly enables
`PV_WIKI_TAVILY_INCLUDE_INTERNAL_HINTS`. `product_id` is never a Tavily query
term.
