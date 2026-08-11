# PV Wiki recovery backups

`pv-wiki-backup` is tailored to the current VPS's existing shared n8n layout.
It makes a consistent custom-format `pg_dump` of the PV Wiki state database,
an online SQLite backup of shared n8n, and stores the n8n encryption config
plus a workflow export. The PostgreSQL archive is validated with
`pg_restore --list`; SQLite is validated with `PRAGMA quick_check`. Defaults
assume the 1Panel PostgreSQL container/database used by the current deployment
and n8n SQLite at `/opt/ai-agents/n8n/data/database.sqlite`. All output is mode
`0600` below a mode `0700` snapshot directory.

This is not a generic backup for the dedicated Compose stack in
`deploy/n8n/compose.yaml`, where n8n uses PostgreSQL. For that topology, replace
or override the installed backup service so it also performs a consistent
`pg_dump` of `n8n-db` and captures the dedicated `n8n_data` volume. Do not use
this script unchanged and assume the n8n PostgreSQL database is protected.

Install on the VPS:

```bash
install -m 0750 deploy/backup/pv-wiki-backup /usr/local/sbin/pv-wiki-backup
install -m 0644 deploy/backup/pv-wiki-backup.service /etc/systemd/system/
install -m 0644 deploy/backup/pv-wiki-backup.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now pv-wiki-backup.timer
systemctl start pv-wiki-backup.service
```

The service prints the completed snapshot path. Verify a snapshot from inside
that directory because `SHA256SUMS` intentionally contains relative names:

```bash
cd /var/backups/pv-wiki/snapshot-YYYYMMDDTHHMMSSZ-SUFFIX
sha256sum -c SHA256SUMS
```

The default target is `/var/backups/pv-wiki`. Override deployment-specific
paths in `/etc/default/pv-wiki-backup`. Local snapshots are recovery copies,
not off-site backups; replicate the completed snapshot directories using the
operator's existing encrypted backup system. Retention is intentionally left
to that system so this script never performs an unsafe recursive deletion.
