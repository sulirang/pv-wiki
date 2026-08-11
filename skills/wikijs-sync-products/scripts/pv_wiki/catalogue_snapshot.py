"""Atomic product-catalogue snapshots for Hermes-owned research.

The legacy ``products`` table remains available to the rollback worker.  Hermes
uses a separate active snapshot so a manual full refresh can atomically replace
the visible catalogue, including removal of source rows, without deleting
legacy attempts or completion records.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .state import StateError, StateStore, canonical_product_json, compute_source_hash


CATALOGUE_TABLE = "product_catalogue_snapshot"
CATALOGUE_METADATA_TABLE = "product_catalogue_snapshot_metadata"

_CREATE_CATALOGUE = f"""
CREATE TABLE IF NOT EXISTS {CATALOGUE_TABLE} (
    product_id TEXT PRIMARY KEY,
    source_hash TEXT NOT NULL,
    source_updated_at TEXT,
    payload_json TEXT NOT NULL,
    refreshed_at TEXT NOT NULL
)
"""

_CREATE_METADATA = f"""
CREATE TABLE IF NOT EXISTS {CATALOGUE_METADATA_TABLE} (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    generation INTEGER NOT NULL CHECK (generation >= 0),
    initialized INTEGER NOT NULL CHECK (initialized IN (0, 1)),
    source_records INTEGER NOT NULL CHECK (source_records >= 0),
    checksum TEXT,
    refreshed_at TEXT,
    source TEXT NOT NULL
)
"""


@dataclass(frozen=True, slots=True)
class CatalogueSnapshotStatus:
    initialized: bool
    generation: int
    source_records: int
    checksum: str | None
    refreshed_at: datetime | None
    source: str


@dataclass(frozen=True, slots=True)
class CatalogueSnapshotResult:
    status: CatalogueSnapshotStatus
    added: int
    changed: int
    removed: int
    unchanged: int


def _utc(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _time_text(value: datetime) -> str:
    return _utc(value).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse_time(value: Any) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise StateError("catalogue snapshot timestamp is invalid")
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError as exc:
        raise StateError("catalogue snapshot timestamp is invalid") from exc


def _status(row: Any) -> CatalogueSnapshotStatus:
    if row is None:
        return CatalogueSnapshotStatus(False, 0, 0, None, None, "uninitialized")
    checksum = row["checksum"]
    return CatalogueSnapshotStatus(
        initialized=bool(row["initialized"]),
        generation=int(row["generation"]),
        source_records=int(row["source_records"]),
        checksum=str(checksum) if checksum is not None else None,
        refreshed_at=_parse_time(row["refreshed_at"]),
        source=str(row["source"]),
    )


def _checksum(rows: Mapping[str, tuple[str, str, str | None]]) -> str:
    digest = hashlib.sha256()
    for product_id in sorted(rows, key=lambda value: (value.casefold(), value)):
        source_hash = rows[product_id][0]
        digest.update(product_id.encode("utf-8"))
        digest.update(b"\0")
        digest.update(source_hash.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _materialize(
    products: Iterable[Any],
) -> dict[str, tuple[str, str, str | None]]:
    rows: dict[str, tuple[str, str, str | None]] = {}
    for product in products:
        payload_json = canonical_product_json(product)
        payload = json.loads(payload_json)
        product_id = str(payload["product_id"]).strip()
        if product_id in rows:
            raise StateError("catalogue refresh contains duplicate product_id values")
        source_updated = payload.get("updated_at") or payload.get("created_at")
        source_updated_text = str(source_updated) if source_updated is not None else None
        rows[product_id] = (
            compute_source_hash(product),
            payload_json,
            source_updated_text,
        )
    if not rows:
        raise StateError("catalogue refresh returned no products; snapshot preserved")
    return rows


def initialize_catalogue_snapshot(state: StateStore) -> CatalogueSnapshotStatus:
    """Create snapshot tables and backfill the existing legacy snapshot once."""

    with state._write_transaction() as connection:
        connection.execute(_CREATE_CATALOGUE)
        connection.execute(_CREATE_METADATA)
        existing = connection.execute(
            f"SELECT * FROM {CATALOGUE_METADATA_TABLE} WHERE singleton = 1"
        ).fetchone()
        if existing is not None:
            return _status(existing)

        legacy_rows = connection.execute(
            """
            SELECT product_id, source_hash, source_updated_at, payload_json,
                   updated_at
            FROM products
            ORDER BY product_id
            """
        ).fetchall()
        snapshot_rows: dict[str, tuple[str, str, str | None]] = {}
        for row in legacy_rows:
            product_id = str(row["product_id"])
            snapshot_rows[product_id] = (
                str(row["source_hash"]),
                str(row["payload_json"]),
                (
                    str(row["source_updated_at"])
                    if row["source_updated_at"] is not None
                    else None
                ),
            )
            connection.execute(
                f"""
                INSERT INTO {CATALOGUE_TABLE} (
                    product_id, source_hash, source_updated_at, payload_json,
                    refreshed_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (product_id) DO NOTHING
                """,
                (
                    product_id,
                    row["source_hash"],
                    row["source_updated_at"],
                    row["payload_json"],
                    row["updated_at"],
                ),
            )

        initialized = bool(snapshot_rows)
        checksum = _checksum(snapshot_rows) if initialized else None
        connection.execute(
            f"""
            INSERT INTO {CATALOGUE_METADATA_TABLE} (
                singleton, generation, initialized, source_records, checksum,
                refreshed_at, source
            ) VALUES (1, 0, ?, ?, ?, NULL, ?)
            """,
            (
                1 if initialized else 0,
                len(snapshot_rows),
                checksum,
                "legacy-backfill" if initialized else "uninitialized",
            ),
        )
        row = connection.execute(
            f"SELECT * FROM {CATALOGUE_METADATA_TABLE} WHERE singleton = 1"
        ).fetchone()
        return _status(row)


def catalogue_snapshot_status(state: StateStore) -> CatalogueSnapshotStatus:
    initialize_catalogue_snapshot(state)
    with state._connection() as connection:
        row = connection.execute(
            f"SELECT * FROM {CATALOGUE_METADATA_TABLE} WHERE singleton = 1"
        ).fetchone()
    return _status(row)


def validate_catalogue_snapshot(products: Iterable[Any]) -> None:
    """Validate a complete source scan before any state table is modified."""

    _materialize(products)


def replace_catalogue_snapshot(
    state: StateStore,
    products: Iterable[Any],
    *,
    source: str = "manual",
    now: datetime | None = None,
) -> CatalogueSnapshotResult:
    """Atomically replace the snapshot after fully validating a non-empty scan."""

    normalized_source = " ".join(str(source).split())
    if not normalized_source or len(normalized_source) > 64:
        raise ValueError("catalogue snapshot source must be 1 to 64 characters")
    incoming = _materialize(products)
    incoming_checksum = _checksum(incoming)
    refreshed_at = _time_text(_utc(now))
    initialize_catalogue_snapshot(state)

    with state._write_transaction() as connection:
        previous_rows = connection.execute(
            f"SELECT product_id, source_hash FROM {CATALOGUE_TABLE}"
        ).fetchall()
        previous = {
            str(row["product_id"]): str(row["source_hash"])
            for row in previous_rows
        }
        added = len(incoming.keys() - previous.keys())
        removed = len(previous.keys() - incoming.keys())
        changed = sum(
            1
            for product_id in incoming.keys() & previous.keys()
            if incoming[product_id][0] != previous[product_id]
        )
        unchanged = len(incoming) - added - changed

        metadata = connection.execute(
            f"SELECT generation FROM {CATALOGUE_METADATA_TABLE} WHERE singleton = 1"
        ).fetchone()
        generation = int(metadata["generation"]) + 1
        connection.execute(f"DELETE FROM {CATALOGUE_TABLE}")
        for product_id in sorted(
            incoming,
            key=lambda value: (value.casefold(), value),
        ):
            source_hash, payload_json, source_updated_at = incoming[product_id]
            connection.execute(
                f"""
                INSERT INTO {CATALOGUE_TABLE} (
                    product_id, source_hash, source_updated_at, payload_json,
                    refreshed_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    product_id,
                    source_hash,
                    source_updated_at,
                    payload_json,
                    refreshed_at,
                ),
            )
        connection.execute(
            f"""
            UPDATE {CATALOGUE_METADATA_TABLE}
            SET generation = ?, initialized = 1, source_records = ?,
                checksum = ?, refreshed_at = ?, source = ?
            WHERE singleton = 1
            """,
            (
                generation,
                len(incoming),
                incoming_checksum,
                refreshed_at,
                normalized_source,
            ),
        )
        row = connection.execute(
            f"SELECT * FROM {CATALOGUE_METADATA_TABLE} WHERE singleton = 1"
        ).fetchone()
        return CatalogueSnapshotResult(
            status=_status(row),
            added=added,
            changed=changed,
            removed=removed,
            unchanged=unchanged,
        )


__all__ = [
    "CATALOGUE_METADATA_TABLE",
    "CATALOGUE_TABLE",
    "CatalogueSnapshotResult",
    "CatalogueSnapshotStatus",
    "catalogue_snapshot_status",
    "initialize_catalogue_snapshot",
    "replace_catalogue_snapshot",
    "validate_catalogue_snapshot",
]
