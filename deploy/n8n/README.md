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
3. Fill the user-owned Tavily, AI, Wiki.js, and read-only catalogue settings
   in `worker.env`. `AI_BASE_URL` is an OpenAI-compatible API base path such as
   `https://provider.example/v1`; choose `AI_MODEL` explicitly. Set
   `PV_WIKI_TRUSTED_SOURCE_DOMAINS_JSON` to a JSON object mapping each exact
   catalogue `brand_code` to its narrow, operator-verified official,
   regulatory, or authorized hostnames. Prefer `PGSSLMODE=verify-full`. For
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

   Attach it to `Sync Catalogue`, `Run One Product`, and `Refresh Homepage`.
   The workflow files intentionally contain no credential ID or secret.

8. Manually run `PV Wiki - Product Cycle` once using the final Wiki.js path
   prefix but private/unpublished visibility. Inspect the exact model match,
   citations, specifications, preserved human text, and worker audit. Use a
   separate state volume if a truly separate staging prefix is required, so a
   staging publication cannot suppress the production product for a year.
   Then manually run the homepage workflow.
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

10. Only after review, publish the two workflows. The product cycle runs every
    four hours at minute 05 and processes one due product; the homepage refresh
    runs daily at 02:35 Asia/Shanghai.
11. Run the security audit, configure an n8n Error Workflow with the
    operator's chosen notification channel, and record the deployed image tags
    and workflow IDs:

    ```bash
    docker compose --env-file .env -f compose.yaml exec -T n8n n8n audit
    ```

The worker has no published host port and exposes only fixed authenticated
operations on the Compose network. n8n's Execute Command node stays excluded,
and the Docker socket is never mounted.

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

Before import, change the three HTTP Request node URLs from
`http://pv-wiki-worker:8080/...` to `http://127.0.0.1:8080/...`. This loopback
form is for a host n8n only; `127.0.0.1` inside an n8n container would address
that container itself.

Keep the worker unexposed to the public Internet. Create the Header Auth
credential in the existing n8n, attach it, run a manual private/unpublished
test, and let the user publish the schedules. Never discover n8n by scanning
unrelated hosts; inspect only the user-authorized VPS and supplied URL.

`Sync Catalogue` and `Refresh Homepage` have bounded retries because they are
idempotent. `Run One Product` deliberately has no HTTP retry: if a long request
times out, retrying it could claim a second product. Product-level backoff is
stored by the worker and the next n8n pulse resumes it.

## Backups and removal

Back up all three persistent stores:

- `n8n_db_data` for workflows, credentials, and executions;
- `n8n_data` for the n8n encryption material and local assets;
- `pv_wiki_state` for product leases, evidence audit, and publication history.

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
