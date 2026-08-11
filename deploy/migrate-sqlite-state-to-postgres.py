#!/usr/bin/env python3
"""Copy a quiescent PV Wiki schema-v9 SQLite state DB to PostgreSQL.

The PostgreSQL URL is read only from PV_WIKI_STATE_DATABASE_URL so it does not
appear in process arguments. The target must be an empty, dedicated database.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from pv_wiki.state import SCHEMA_VERSION, StateStore


TABLES: dict[str, tuple[str, ...]] = {
    "products": (
        "product_id",
        "source_hash",
        "source_updated_at",
        "payload_json",
        "status",
        "next_run_at",
        "consecutive_failures",
        "lease_token",
        "lease_owner",
        "lease_until",
        "leased_source_hash",
        "reschedule_requested",
        "last_attempt_at",
        "last_success_at",
        "last_outcome",
        "last_error",
        "created_at",
        "updated_at",
        "content_failure_cutoff_attempt_id",
        "leased_from_status",
        "leased_from_next_run_at",
    ),
    "attempts": (
        "attempt_id",
        "product_id",
        "source_hash",
        "lease_token",
        "worker_id",
        "started_at",
        "lease_until",
        "finished_at",
        "outcome",
        "error",
        "details_json",
        "payload_json",
        "search_started_at",
        "search_urls_json",
        "search_usage_json",
        "extract_started_at",
        "extract_urls_json",
        "extract_usage_json",
        "extract_success_urls_json",
    ),
    "research_actions": (
        "action_id",
        "attempt_id",
        "round_number",
        "action",
        "status",
        "request_fingerprint",
        "started_at",
        "finished_at",
        "result_summary_json",
        "credits",
        "error",
        "scope_fingerprint",
    ),
    "requeue_events": (
        "event_id",
        "product_id",
        "cutoff_attempt_id",
        "previous_status",
        "previous_outcome",
        "reason",
        "attempted_after",
        "attempted_before",
        "requeued_at",
    ),
}

IDENTITIES = {
    "attempts": "attempt_id",
    "research_actions": "action_id",
    "requeue_events": "event_id",
}

ADVISORY_LOCK = 8_604_293_015


def _row_digest(rows: list[tuple[Any, ...]]) -> str:
    digest = hashlib.sha256()
    for row in rows:
        encoded = json.dumps(
            row,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def _source_rows(
    connection: sqlite3.Connection,
    table: str,
    columns: tuple[str, ...],
) -> list[tuple[Any, ...]]:
    column_list = ", ".join(columns)
    order_column = columns[0]
    return [
        tuple(row)
        for row in connection.execute(
            f"SELECT {column_list} FROM {table} ORDER BY {order_column}"
        ).fetchall()
    ]


def _target_rows(
    connection: psycopg.Connection[Any],
    table: str,
    columns: tuple[str, ...],
) -> list[tuple[Any, ...]]:
    column_list = ", ".join(columns)
    order_column = columns[0]
    rows = connection.execute(
        f"SELECT {column_list} FROM {table} ORDER BY {order_column}"
    ).fetchall()
    return [tuple(row[column] for column in columns) for row in rows]


def migrate(sqlite_path: Path, database_url: str) -> dict[str, Any]:
    source = sqlite3.connect(
        f"file:{sqlite_path}?mode=ro",
        uri=True,
    )
    source.row_factory = sqlite3.Row
    try:
        quick_check = source.execute("PRAGMA quick_check").fetchone()
        if quick_check is None or quick_check[0] != "ok":
            raise RuntimeError(f"SQLite quick_check failed: {quick_check!r}")
        foreign_key_errors = source.execute(
            "PRAGMA foreign_key_check"
        ).fetchall()
        if foreign_key_errors:
            raise RuntimeError(
                f"SQLite foreign_key_check found {len(foreign_key_errors)} errors"
            )
        version = int(source.execute("PRAGMA user_version").fetchone()[0])
        if version != SCHEMA_VERSION:
            raise RuntimeError(
                f"SQLite schema is {version}; expected {SCHEMA_VERSION}"
            )
        active_leases = int(
            source.execute(
                "SELECT COUNT(*) FROM products WHERE status = 'leased'"
            ).fetchone()[0]
        )
        unfinished_attempts = int(
            source.execute(
                "SELECT COUNT(*) FROM attempts WHERE finished_at IS NULL"
            ).fetchone()[0]
        )
        if active_leases or unfinished_attempts:
            raise RuntimeError(
                "source is not quiescent: "
                f"{active_leases} active leases, "
                f"{unfinished_attempts} unfinished attempts"
            )

        source_rows = {
            table: _source_rows(source, table, columns)
            for table, columns in TABLES.items()
        }
        source_digests = {
            table: _row_digest(rows)
            for table, rows in source_rows.items()
        }

        with StateStore(database_url) as store:
            safe_location = store.location

        with psycopg.connect(
            database_url,
            autocommit=False,
            row_factory=dict_row,
            connect_timeout=5,
        ) as target:
            target.execute(
                "SELECT pg_advisory_xact_lock(%s)",
                (ADVISORY_LOCK,),
            )
            for table in TABLES:
                count = int(
                    target.execute(
                        f"SELECT COUNT(*) AS count FROM {table}"
                    ).fetchone()["count"]
                )
                if count:
                    raise RuntimeError(
                        f"target table {table} is not empty ({count} rows)"
                    )

            for table, columns in TABLES.items():
                rows = source_rows[table]
                if not rows:
                    continue
                column_list = ", ".join(columns)
                placeholders = ", ".join("%s" for _ in columns)
                target.cursor().executemany(
                    f"INSERT INTO {table} ({column_list}) "
                    f"VALUES ({placeholders})",
                    rows,
                )

            for table, identity_column in IDENTITIES.items():
                target.execute(
                    """
                    SELECT setval(
                        pg_get_serial_sequence(%s, %s),
                        COALESCE(MAX_VALUE.maximum_id, 1),
                        MAX_VALUE.maximum_id IS NOT NULL
                    )
                    FROM (
                    """
                    + f"SELECT MAX({identity_column}) AS maximum_id FROM {table}"
                    + """
                    ) AS MAX_VALUE
                    """,
                    (table, identity_column),
                )

            target_rows = {
                table: _target_rows(target, table, columns)
                for table, columns in TABLES.items()
            }
            target_digests = {
                table: _row_digest(rows)
                for table, rows in target_rows.items()
            }
            for table in TABLES:
                if len(source_rows[table]) != len(target_rows[table]):
                    raise RuntimeError(f"row count mismatch for {table}")
                if source_digests[table] != target_digests[table]:
                    raise RuntimeError(f"content digest mismatch for {table}")

        return {
            "ok": True,
            "source": str(sqlite_path),
            "target": safe_location,
            "schema_version": version,
            "tables": {
                table: {
                    "rows": len(source_rows[table]),
                    "sha256": source_digests[table],
                }
                for table in TABLES
            },
        }
    finally:
        source.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--sqlite",
        type=Path,
        required=True,
        help="absolute path to the quiescent schema-v9 SQLite database",
    )
    args = parser.parse_args()
    sqlite_path = args.sqlite.expanduser().resolve()
    if not sqlite_path.is_file():
        parser.error("--sqlite must name an existing file")
    database_url = os.getenv("PV_WIKI_STATE_DATABASE_URL", "").strip()
    if not database_url:
        parser.error("PV_WIKI_STATE_DATABASE_URL is required")
    result = migrate(sqlite_path, database_url)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
