"""Durable, idempotent scheduler state for product wiki synchronisation.

SQLite owns only orchestration state; the PostgreSQL catalogue remains the
source of truth.  Every mutating scheduler operation uses ``BEGIN IMMEDIATE``
so two workers can safely share the same state database.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import urllib.parse
import uuid
from collections.abc import Iterable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, is_dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 4
BACKOFF_DAYS = (30, 90, 180)
TRANSIENT_BACKOFF_HOURS = (1, 6, 24)
TRANSIENT_OUTCOMES = frozenset(
    {
        "tavily_error",
        "ai_error",
        "wikijs_error",
        "error",
        "lease_expired",
        "invalid_decision",
        "configuration_error",
        "publish_error",
    }
)
SYNC_REFRESH_DAYS = 365
PRODUCT_SOURCE_FIELDS = (
    "product_id",
    "brand_code",
    "family_code",
    "product_name",
    "unit_of_measure",
    "created_at",
    "updated_at",
)
PRODUCT_STATUSES = ("due", "leased", "backoff", "synced")

_CREATE_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS products (
        product_id TEXT PRIMARY KEY,
        source_hash TEXT NOT NULL,
        source_updated_at TEXT,
        payload_json TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('due', 'leased', 'backoff', 'synced')),
        next_run_at TEXT NOT NULL,
        consecutive_failures INTEGER NOT NULL DEFAULT 0 CHECK (consecutive_failures >= 0),
        lease_token TEXT UNIQUE,
        lease_owner TEXT,
        lease_until TEXT,
        leased_source_hash TEXT,
        reschedule_requested INTEGER NOT NULL DEFAULT 0
            CHECK (reschedule_requested IN (0, 1)),
        last_attempt_at TEXT,
        last_success_at TEXT,
        last_outcome TEXT,
        last_error TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        CHECK (
            (status = 'leased' AND lease_token IS NOT NULL
                               AND lease_owner IS NOT NULL
                               AND lease_until IS NOT NULL
                               AND leased_source_hash IS NOT NULL)
            OR
            (status <> 'leased' AND lease_token IS NULL
                                AND lease_owner IS NULL
                                AND lease_until IS NULL
                                AND leased_source_hash IS NULL)
        )
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS products_due_idx
        ON products(status, next_run_at, updated_at)
    """,
    """
    CREATE TABLE IF NOT EXISTS attempts (
        attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
        product_id TEXT NOT NULL REFERENCES products(product_id) ON DELETE CASCADE,
        source_hash TEXT NOT NULL,
        lease_token TEXT NOT NULL UNIQUE,
        worker_id TEXT NOT NULL,
        started_at TEXT NOT NULL,
        lease_until TEXT NOT NULL,
        finished_at TEXT,
        outcome TEXT,
        error TEXT,
        details_json TEXT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS attempts_product_idx
        ON attempts(product_id, attempt_id)
    """,
)


class StateError(RuntimeError):
    """Base class for durable scheduler state errors."""


class SchemaVersionError(StateError):
    """Raised when the database was created by a newer implementation."""


class UnknownProductError(StateError):
    """Raised when a product key is not present in scheduler state."""


class LeaseLostError(StateError):
    """Raised when an outcome is submitted for an invalid or expired lease."""


class AttemptBudgetError(StateError):
    """Raised when an attempt's one-shot web budget or evidence order is invalid."""


@dataclass(frozen=True, slots=True)
class UpsertResult:
    product_id: str
    source_hash: str
    created: bool
    changed: bool
    rescheduled: bool
    status: str


@dataclass(frozen=True, slots=True)
class ProductState:
    product_id: str
    source_hash: str
    source_updated_at: datetime | None
    payload: dict[str, Any]
    status: str
    next_run_at: datetime
    consecutive_failures: int
    lease_token: str | None
    lease_owner: str | None
    lease_until: datetime | None
    leased_source_hash: str | None
    reschedule_requested: bool
    last_attempt_at: datetime | None
    last_success_at: datetime | None
    last_outcome: str | None
    last_error: str | None


@dataclass(frozen=True, slots=True)
class PrecheckResult:
    product_id: str
    ready: bool
    reason: str
    status: str | None
    source_hash: str | None
    next_run_at: datetime | None

    @property
    def due(self) -> bool:
        """Whether a not-yet-leased product is due to be scheduled."""

        return self.ready and self.reason == "due"


@dataclass(frozen=True, slots=True)
class Lease:
    attempt_id: int
    product_id: str
    source_hash: str
    payload: dict[str, Any]
    token: str
    worker_id: str
    leased_until: datetime


# A descriptive alias for callers that prefer to distinguish this from other
# application leases.
TaskLease = Lease


@dataclass(frozen=True, slots=True)
class OutcomeResult:
    product_id: str
    outcome: str
    accepted: bool
    status: str
    next_run_at: datetime
    consecutive_failures: int

    @property
    def next_attempt_at(self) -> datetime:
        """Compatibility name for queue/CLI callers."""

        return self.next_run_at


@dataclass(frozen=True, slots=True)
class AttemptRecord:
    attempt_id: int
    product_id: str
    source_hash: str
    lease_token: str
    worker_id: str
    started_at: datetime
    lease_until: datetime
    finished_at: datetime | None
    outcome: str | None
    error: str | None
    details: Any
    payload: dict[str, Any]
    search_started_at: datetime | None
    search_urls: list[str] | None
    search_usage: dict[str, Any] | None
    extract_started_at: datetime | None
    extract_urls: list[str] | None
    extract_success_urls: list[str] | None
    extract_usage: dict[str, Any] | None


@dataclass(frozen=True, slots=True)
class PublishedProduct:
    """The latest successful Wiki publication metadata for one product."""

    product_id: str
    payload: dict[str, Any]
    decision: dict[str, Any]
    wiki_path: str | None
    published_at: datetime


def _utc(value: datetime | None = None) -> datetime:
    value = value or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _time_text(value: datetime) -> str:
    # A fixed-width UTC representation sorts chronologically as SQLite TEXT.
    return _utc(value).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse_time(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
        tzinfo=timezone.utc
    )


def _normalise_json(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        value = asdict(value)
    if isinstance(value, Mapping):
        return {
            str(key): _normalise_json(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_normalise_json(item) for item in value]
    if isinstance(value, datetime):
        return _time_text(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, Enum):
        return _normalise_json(value.value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    raise TypeError(f"unsupported product value for canonical JSON: {type(value)!r}")


def _canonical_json(value: Any) -> str:
    return json.dumps(
        _normalise_json(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _canonical_url(value: Any) -> str:
    """Return a conservative canonical form for attempt-local URL binding."""

    if not isinstance(value, str) or not value.strip():
        raise ValueError("evidence URLs must be non-empty strings")
    try:
        parts = urllib.parse.urlsplit(value.strip())
        port = parts.port
    except ValueError as exc:
        raise ValueError("invalid evidence URL") from exc
    scheme = parts.scheme.casefold()
    hostname = (parts.hostname or "").rstrip(".").casefold()
    if scheme not in {"http", "https"} or not hostname:
        raise ValueError("evidence URLs must be absolute HTTP(S) URLs")
    if parts.username is not None or parts.password is not None:
        raise ValueError("evidence URLs cannot contain credentials")
    if port not in {None, 80, 443}:
        raise ValueError("evidence URLs cannot use non-standard ports")
    default_port = (scheme == "http" and port in {None, 80}) or (
        scheme == "https" and port in {None, 443}
    )
    host = f"[{hostname}]" if ":" in hostname else hostname
    netloc = host if default_port else f"{host}:{port}"
    return urllib.parse.urlunsplit(
        (scheme, netloc, parts.path or "/", parts.query, "")
    )


def _canonical_urls(
    values: Sequence[str],
    *,
    maximum: int | None = None,
    require_nonempty: bool = False,
) -> list[str]:
    if isinstance(values, (str, bytes, bytearray)) or not isinstance(values, Sequence):
        raise TypeError("urls must be a sequence of strings")
    if maximum is not None and len(values) > maximum:
        raise ValueError(f"at most {maximum} URLs are allowed")
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = _canonical_url(value)
        if normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    if require_nonempty and not result:
        raise ValueError("at least one URL is required")
    return result


def _usage_json(usage: Mapping[str, Any]) -> str:
    if not isinstance(usage, Mapping):
        raise TypeError("usage must be a mapping")
    return _canonical_json(dict(usage))


def _product_mapping(product: Any) -> dict[str, Any]:
    if isinstance(product, Mapping):
        raw = dict(product)
    elif hasattr(product, "as_dict"):
        raw = dict(product.as_dict())
    elif is_dataclass(product) and not isinstance(product, type):
        raw = asdict(product)
    else:
        raise TypeError("product must be a mapping, dataclass, or expose as_dict()")

    if raw.get("product_id") is None or str(raw["product_id"]).strip() == "":
        raise ValueError("product_id is required")
    # Ignore unrelated caller metadata: only the fixed PostgreSQL source fields
    # determine whether catalogue content changed.
    return {field: raw.get(field) for field in PRODUCT_SOURCE_FIELDS}


def canonical_product_json(product: Any) -> str:
    """Return the stable JSON representation used for persistence and hashing."""

    return _canonical_json(_product_mapping(product))


def compute_source_hash(product: Any) -> str:
    """Hash the fixed source fields for deterministic change detection."""

    return hashlib.sha256(canonical_product_json(product).encode("utf-8")).hexdigest()


def backoff_days(consecutive_failures: int) -> int:
    """Return the 30/90/180-day retry delay for a one-based failure count."""

    if consecutive_failures <= 0:
        raise ValueError("consecutive_failures must be positive")
    return BACKOFF_DAYS[min(consecutive_failures - 1, len(BACKOFF_DAYS) - 1)]


def retry_delay(outcome: str, consecutive_failures: int) -> timedelta:
    """Return the outcome-specific delay for a one-based failure count."""

    if consecutive_failures <= 0:
        raise ValueError("consecutive_failures must be positive")
    if outcome == "wikijs_conflict":
        return timedelta(hours=24)
    if outcome in TRANSIENT_OUTCOMES:
        index = min(consecutive_failures - 1, len(TRANSIENT_BACKOFF_HOURS) - 1)
        return timedelta(hours=TRANSIENT_BACKOFF_HOURS[index])
    return timedelta(days=backoff_days(consecutive_failures))


class StateStore:
    """SQLite-backed product queue, lease manager, and attempt audit log."""

    @staticmethod
    def _create_parent_directories(parent: Path) -> None:
        missing: list[Path] = []
        current = parent
        while not current.exists():
            missing.append(current)
            if current.parent == current:
                break
            current = current.parent
        for directory in reversed(missing):
            try:
                directory.mkdir(mode=0o700)
            except FileExistsError:
                # A concurrent StateStore created it; it is not ours to chmod.
                continue
            if os.name == "posix":
                try:
                    directory.chmod(0o700)
                except OSError as exc:
                    raise StateError(
                        f"cannot secure newly created state directory: {directory}"
                    ) from exc

    @staticmethod
    def _create_database_file(path: Path) -> None:
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            return
        try:
            os.close(descriptor)
            if os.name == "posix":
                path.chmod(0o600)
        except OSError as exc:
            raise StateError(f"cannot secure newly created state file: {path}") from exc

    def __init__(self, path: str | Path, *, timeout: float = 5.0) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        supplied_path = str(path)
        self.path = supplied_path
        self._timeout = timeout
        self._keeper: sqlite3.Connection | None = None
        if supplied_path == ":memory:":
            # Methods intentionally use short-lived connections.  A shared-cache
            # URI plus a keeper preserves those semantics for in-memory tests.
            self._database = f"file:pv_wiki_state_{uuid.uuid4().hex}?mode=memory&cache=shared"
            self._uri = True
            self._keeper = self._new_connection()
        else:
            database_path = Path(supplied_path).expanduser()
            self._create_parent_directories(database_path.parent)
            self._create_database_file(database_path)
            self.path = str(database_path)
            self._database = str(database_path)
            self._uri = False
        self.migrate()

    def _new_connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self._database,
            timeout=self._timeout,
            isolation_level=None,
            uri=self._uri,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {int(self._timeout * 1000)}")
        return connection

    @contextmanager
    def _connection(self) -> Any:
        connection = self._new_connection()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _write_transaction(self) -> Any:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()

    def close(self) -> None:
        if self._keeper is not None:
            self._keeper.close()
            self._keeper = None

    def __enter__(self) -> "StateStore":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    def migrate(self) -> int:
        """Apply known, idempotent migrations and return the schema version."""

        with self._write_transaction() as connection:
            current = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if current > SCHEMA_VERSION:
                raise SchemaVersionError(
                    f"state schema {current} is newer than supported version {SCHEMA_VERSION}"
                )
            if current < 1:
                for statement in _CREATE_SCHEMA:
                    connection.execute(statement)
                connection.execute("PRAGMA user_version = 1")
                current = 1
            if current < 2:
                # Leases must retain the exact product snapshot they were
                # issued for.  Otherwise a concurrent PostgreSQL change would
                # make token lookup combine the old hash with a new payload.
                connection.execute(
                    "ALTER TABLE attempts ADD COLUMN payload_json TEXT"
                )
                connection.execute(
                    """
                    UPDATE attempts
                    SET payload_json = (
                        SELECT products.payload_json
                        FROM products
                        WHERE products.product_id = attempts.product_id
                    )
                    WHERE payload_json IS NULL
                    """
                )
                connection.execute("PRAGMA user_version = 2")
                current = 2
            if current < 3:
                for column in (
                    "search_started_at TEXT",
                    "search_urls_json TEXT",
                    "search_usage_json TEXT",
                    "extract_started_at TEXT",
                    "extract_urls_json TEXT",
                    "extract_usage_json TEXT",
                ):
                    # Column definitions are constants owned by this migration.
                    connection.execute(f"ALTER TABLE attempts ADD COLUMN {column}")
                connection.execute("PRAGMA user_version = 3")
                current = 3
            if current < 4:
                # Only URLs that Tavily actually extracted may become evidence.
                # Existing active attempts fail closed because their successful
                # subset cannot be reconstructed from the v3 audit record.
                connection.execute(
                    "ALTER TABLE attempts ADD COLUMN extract_success_urls_json TEXT"
                )
                connection.execute("PRAGMA user_version = 4")
                current = 4
        return current

    @property
    def schema_version(self) -> int:
        with self._connection() as connection:
            return int(connection.execute("PRAGMA user_version").fetchone()[0])

    def upsert_product(
        self,
        product: Any,
        *,
        source_hash: str | None = None,
        now: datetime | None = None,
    ) -> UpsertResult:
        """Insert/update a source product and make new or changed rows due now."""

        timestamp = _utc(now)
        with self._write_transaction() as connection:
            return self._upsert_product(
                connection,
                product,
                timestamp=timestamp,
                source_hash=source_hash,
            )

    def upsert_products(
        self,
        products: Iterable[Any],
        *,
        now: datetime | None = None,
    ) -> list[UpsertResult]:
        """Upsert a PostgreSQL batch in one SQLite transaction."""

        timestamp = _utc(now)
        results: list[UpsertResult] = []
        with self._write_transaction() as connection:
            for product in products:
                results.append(
                    self._upsert_product(
                        connection,
                        product,
                        timestamp=timestamp,
                        source_hash=None,
                    )
                )
        return results

    def _upsert_product(
        self,
        connection: sqlite3.Connection,
        product: Any,
        *,
        timestamp: datetime,
        source_hash: str | None,
    ) -> UpsertResult:
        mapping = _product_mapping(product)
        product_id = str(mapping["product_id"])
        payload_json = _canonical_json(mapping)
        calculated_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        if source_hash is None:
            source_hash = calculated_hash
        elif not isinstance(source_hash, str) or not source_hash.strip():
            raise ValueError("source_hash must be a non-empty string")

        source_updated = mapping.get("updated_at") or mapping.get("created_at")
        source_updated_text = (
            _time_text(source_updated)
            if isinstance(source_updated, datetime)
            else None
        )
        now_text = _time_text(timestamp)
        existing = connection.execute(
            "SELECT source_hash, status FROM products WHERE product_id = ?",
            (product_id,),
        ).fetchone()

        if existing is None:
            connection.execute(
                """
                INSERT INTO products (
                    product_id, source_hash, source_updated_at, payload_json,
                    status, next_run_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'due', ?, ?, ?)
                """,
                (
                    product_id,
                    source_hash,
                    source_updated_text,
                    payload_json,
                    now_text,
                    now_text,
                    now_text,
                ),
            )
            return UpsertResult(product_id, source_hash, True, True, True, "due")

        if existing["source_hash"] == source_hash:
            # Keeping the canonical payload current is useful if a caller
            # supplies an externally calculated-but-equivalent source hash.
            connection.execute(
                """
                UPDATE products
                SET payload_json = ?, source_updated_at = ?
                WHERE product_id = ?
                """,
                (payload_json, source_updated_text, product_id),
            )
            return UpsertResult(
                product_id,
                source_hash,
                False,
                False,
                False,
                existing["status"],
            )

        if existing["status"] == "leased":
            # Do not hand the same product to a second worker.  The active
            # lease's source hash will fail precheck/outcome acceptance and the
            # newest payload is scheduled immediately afterwards.
            connection.execute(
                """
                UPDATE products
                SET source_hash = ?, source_updated_at = ?, payload_json = ?,
                    reschedule_requested = 1, consecutive_failures = 0,
                    last_error = NULL, updated_at = ?
                WHERE product_id = ?
                """,
                (
                    source_hash,
                    source_updated_text,
                    payload_json,
                    now_text,
                    product_id,
                ),
            )
            return UpsertResult(product_id, source_hash, False, True, True, "leased")

        connection.execute(
            """
            UPDATE products
            SET source_hash = ?, source_updated_at = ?, payload_json = ?,
                status = 'due', next_run_at = ?, consecutive_failures = 0,
                lease_token = NULL, lease_owner = NULL, lease_until = NULL,
                leased_source_hash = NULL, reschedule_requested = 0,
                last_outcome = 'source_changed', last_error = NULL,
                updated_at = ?
            WHERE product_id = ?
            """,
            (
                source_hash,
                source_updated_text,
                payload_json,
                now_text,
                now_text,
                product_id,
            ),
        )
        return UpsertResult(product_id, source_hash, False, True, True, "due")

    def get_product(self, product_id: Any) -> ProductState | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM products WHERE product_id = ?", (str(product_id),)
            ).fetchone()
        return self._product_state(row) if row is not None else None

    def list_due(
        self,
        *,
        now: datetime | None = None,
        limit: int = 100,
    ) -> list[ProductState]:
        """Return runnable products after reclaiming expired worker leases."""

        if isinstance(limit, bool) or not isinstance(limit, int):
            raise TypeError("limit must be an integer")
        if limit <= 0:
            raise ValueError("limit must be greater than zero")
        timestamp = _utc(now)
        self.reclaim_expired_leases(now=timestamp)
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM products
                WHERE status IN ('due', 'backoff', 'synced')
                  AND next_run_at <= ?
                ORDER BY
                    CASE status WHEN 'due' THEN 0 WHEN 'backoff' THEN 1 ELSE 2 END,
                    next_run_at,
                    updated_at,
                    product_id
                LIMIT ?
                """,
                (_time_text(timestamp), limit),
            ).fetchall()
        return [self._product_state(row) for row in rows]

    def is_due(self, product_id: Any, *, now: datetime | None = None) -> bool:
        return self.precheck(str(product_id), now=now).due

    def precheck(
        self,
        product: Any,
        *,
        expected_source_hash: str | None = None,
        lease_token: str | None = None,
        now: datetime | None = None,
    ) -> PrecheckResult:
        """Check due state or validate a lease immediately before web work.

        Passing a :class:`Lease` automatically checks its token and source hash.
        This inexpensive check should be made before spending a Tavily request.
        """

        if isinstance(product, Lease):
            product_id = product.product_id
            expected_source_hash = expected_source_hash or product.source_hash
            lease_token = lease_token or product.token
        else:
            product_id = str(product)

        timestamp = _utc(now)
        self.reclaim_expired_leases(now=timestamp)
        row = self.get_product(product_id)
        if row is None:
            return PrecheckResult(product_id, False, "missing", None, None, None)

        if expected_source_hash is not None and row.source_hash != expected_source_hash:
            return PrecheckResult(
                product_id,
                False,
                "source_changed",
                row.status,
                row.source_hash,
                row.next_run_at,
            )

        if lease_token is not None:
            valid = (
                row.status == "leased"
                and row.lease_token == lease_token
                and row.leased_source_hash == row.source_hash
                and not row.reschedule_requested
                and row.lease_until is not None
                and row.lease_until > timestamp
            )
            return PrecheckResult(
                product_id,
                valid,
                "leased" if valid else "lease_lost",
                row.status,
                row.source_hash,
                row.next_run_at,
            )

        if row.status == "leased":
            return PrecheckResult(
                product_id,
                False,
                "leased_by_other",
                row.status,
                row.source_hash,
                row.next_run_at,
            )
        due = row.next_run_at <= timestamp
        return PrecheckResult(
            product_id,
            due,
            "due" if due else "not_due",
            row.status,
            row.source_hash,
            row.next_run_at,
        )

    def lease_next(
        self,
        worker_id: str,
        *,
        lease_seconds: int = 900,
        now: datetime | None = None,
    ) -> Lease | None:
        """Atomically reclaim expiry and lease exactly one due product."""

        if not isinstance(worker_id, str) or not worker_id.strip():
            raise ValueError("worker_id must be a non-empty string")
        if (
            isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, int)
            or lease_seconds <= 0
        ):
            raise ValueError("lease_seconds must be a positive integer")
        timestamp = _utc(now)
        now_text = _time_text(timestamp)
        lease_until = timestamp + timedelta(seconds=lease_seconds)
        lease_until_text = _time_text(lease_until)

        with self._write_transaction() as connection:
            self._reclaim_expired(connection, timestamp)
            row = connection.execute(
                """
                SELECT * FROM products
                WHERE status IN ('due', 'backoff', 'synced')
                  AND next_run_at <= ?
                ORDER BY
                    CASE status WHEN 'due' THEN 0 WHEN 'backoff' THEN 1 ELSE 2 END,
                    next_run_at,
                    updated_at,
                    product_id
                LIMIT 1
                """,
                (now_text,),
            ).fetchone()
            if row is None:
                return None

            token = uuid.uuid4().hex
            updated = connection.execute(
                """
                UPDATE products
                SET status = 'leased', lease_token = ?, lease_owner = ?,
                    lease_until = ?, leased_source_hash = source_hash,
                    reschedule_requested = 0, last_attempt_at = ?, updated_at = ?
                WHERE product_id = ?
                  AND status IN ('due', 'backoff', 'synced')
                  AND next_run_at <= ?
                """,
                (
                    token,
                    worker_id,
                    lease_until_text,
                    now_text,
                    now_text,
                    row["product_id"],
                    now_text,
                ),
            )
            if updated.rowcount != 1:  # defensive; BEGIN IMMEDIATE should prevent it
                return None
            attempt = connection.execute(
                """
                INSERT INTO attempts (
                    product_id, source_hash, lease_token, worker_id,
                    started_at, lease_until, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["product_id"],
                    row["source_hash"],
                    token,
                    worker_id,
                    now_text,
                    lease_until_text,
                    row["payload_json"],
                ),
            )
            attempt_id = int(attempt.lastrowid)

        return Lease(
            attempt_id=attempt_id,
            product_id=row["product_id"],
            source_hash=row["source_hash"],
            payload=json.loads(row["payload_json"]),
            token=token,
            worker_id=worker_id,
            leased_until=lease_until,
        )

    def get_by_lease(
        self,
        lease_token: str,
        *,
        now: datetime | None = None,
    ) -> Lease | None:
        """Resolve an active token to the exact product snapshot it leased."""

        if not isinstance(lease_token, str) or not lease_token:
            raise ValueError("lease_token must be a non-empty string")
        self.reclaim_expired_leases(now=now)
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT
                    p.product_id,
                    p.lease_token,
                    p.lease_owner,
                    p.lease_until,
                    a.attempt_id,
                    a.source_hash,
                    a.payload_json
                FROM products AS p
                JOIN attempts AS a ON a.lease_token = p.lease_token
                WHERE p.status = 'leased' AND p.lease_token = ?
                  AND a.finished_at IS NULL
                """,
                (lease_token,),
            ).fetchone()
        if row is None:
            return None
        return Lease(
            attempt_id=int(row["attempt_id"]),
            product_id=row["product_id"],
            source_hash=row["source_hash"],
            payload=json.loads(row["payload_json"]),
            token=row["lease_token"],
            worker_id=row["lease_owner"],
            leased_until=_parse_time(row["lease_until"]),  # type: ignore[arg-type]
        )

    @staticmethod
    def _token_for(lease_or_token: Lease | str) -> str:
        if isinstance(lease_or_token, Lease):
            token = lease_or_token.token
        elif isinstance(lease_or_token, str):
            token = lease_or_token
        else:
            raise TypeError("lease_or_token must be a Lease or string token")
        if not token:
            raise ValueError("lease token is required")
        return token

    def _active_attempt(
        self,
        connection: sqlite3.Connection,
        lease_or_token: Lease | str,
        timestamp: datetime,
    ) -> sqlite3.Row:
        """Return an unfinished attempt only while its exact source lease is valid."""

        token = self._token_for(lease_or_token)
        row = connection.execute(
            """
            SELECT
                a.*,
                p.source_hash AS current_source_hash,
                p.leased_source_hash AS current_leased_source_hash,
                p.reschedule_requested AS current_reschedule_requested
            FROM attempts AS a
            JOIN products AS p ON p.product_id = a.product_id
            WHERE a.lease_token = ?
              AND a.finished_at IS NULL
              AND p.status = 'leased'
              AND p.lease_token = a.lease_token
              AND p.lease_until > ?
            """,
            (token, _time_text(timestamp)),
        ).fetchone()
        if row is None:
            raise LeaseLostError("lease is missing, expired, or already completed")
        if (
            row["current_reschedule_requested"]
            or row["current_source_hash"] != row["current_leased_source_hash"]
            or row["current_leased_source_hash"] != row["source_hash"]
        ):
            raise LeaseLostError("lease source changed after it was claimed")
        if isinstance(lease_or_token, Lease) and (
            lease_or_token.product_id != row["product_id"]
            or lease_or_token.source_hash != row["source_hash"]
            or lease_or_token.attempt_id != row["attempt_id"]
        ):
            raise LeaseLostError("lease identity does not match durable state")
        return row

    def begin_search(
        self,
        lease_or_token: Lease | str,
        *,
        now: datetime | None = None,
    ) -> datetime:
        """Atomically consume this lease's single Tavily search allowance."""

        timestamp = _utc(now)
        with self._write_transaction() as connection:
            row = self._active_attempt(connection, lease_or_token, timestamp)
            if row["search_started_at"] is not None:
                raise AttemptBudgetError("search allowance is already consumed")
            changed = connection.execute(
                """
                UPDATE attempts
                SET search_started_at = ?
                WHERE attempt_id = ? AND search_started_at IS NULL
                """,
                (_time_text(timestamp), row["attempt_id"]),
            )
            if changed.rowcount != 1:  # defensive under BEGIN IMMEDIATE
                raise AttemptBudgetError("search allowance is already consumed")
        return timestamp

    def finish_search(
        self,
        lease_or_token: Lease | str,
        urls: Sequence[str],
        usage: Mapping[str, Any],
        *,
        now: datetime | None = None,
    ) -> list[str]:
        """Persist the candidates and usage produced by the begun search."""

        normalized_urls = _canonical_urls(urls)
        urls_json = _canonical_json(normalized_urls)
        usage_json = _usage_json(usage)
        timestamp = _utc(now)
        with self._write_transaction() as connection:
            row = self._active_attempt(connection, lease_or_token, timestamp)
            if row["search_started_at"] is None:
                raise AttemptBudgetError("search must begin before it can finish")
            if row["search_urls_json"] is not None:
                if (
                    row["search_urls_json"] == urls_json
                    and row["search_usage_json"] == usage_json
                ):
                    return normalized_urls
                raise AttemptBudgetError("search audit is already completed")
            connection.execute(
                """
                UPDATE attempts
                SET search_urls_json = ?, search_usage_json = ?
                WHERE attempt_id = ? AND search_urls_json IS NULL
                """,
                (urls_json, usage_json, row["attempt_id"]),
            )
        return normalized_urls

    def begin_extract(
        self,
        lease_or_token: Lease | str,
        urls: Sequence[str],
        *,
        now: datetime | None = None,
    ) -> list[str]:
        """Consume one extract allowance for at most five searched candidates."""

        normalized_urls = _canonical_urls(
            urls,
            maximum=5,
            require_nonempty=True,
        )
        timestamp = _utc(now)
        with self._write_transaction() as connection:
            row = self._active_attempt(connection, lease_or_token, timestamp)
            if row["search_urls_json"] is None:
                raise AttemptBudgetError("search must finish before extract begins")
            if row["extract_started_at"] is not None:
                raise AttemptBudgetError("extract allowance is already consumed")
            search_urls = set(json.loads(row["search_urls_json"]))
            outside = [url for url in normalized_urls if url not in search_urls]
            if outside:
                raise AttemptBudgetError(
                    "extract URLs must belong to this lease's search candidates"
                )
            changed = connection.execute(
                """
                UPDATE attempts
                SET extract_started_at = ?, extract_urls_json = ?
                WHERE attempt_id = ? AND extract_started_at IS NULL
                """,
                (
                    _time_text(timestamp),
                    _canonical_json(normalized_urls),
                    row["attempt_id"],
                ),
            )
            if changed.rowcount != 1:  # defensive under BEGIN IMMEDIATE
                raise AttemptBudgetError("extract allowance is already consumed")
        return normalized_urls

    def finish_extract(
        self,
        lease_or_token: Lease | str,
        successful_urls: Sequence[str],
        usage: Mapping[str, Any],
        *,
        now: datetime | None = None,
    ) -> list[str]:
        """Persist usage and the successfully extracted evidence URL subset."""

        normalized_success_urls = _canonical_urls(successful_urls)
        success_urls_json = _canonical_json(normalized_success_urls)
        usage_json = _usage_json(usage)
        timestamp = _utc(now)
        with self._write_transaction() as connection:
            row = self._active_attempt(connection, lease_or_token, timestamp)
            if row["extract_started_at"] is None or row["extract_urls_json"] is None:
                raise AttemptBudgetError("extract must begin before it can finish")
            submitted_urls = list(json.loads(row["extract_urls_json"]))
            outside = [
                url for url in normalized_success_urls if url not in set(submitted_urls)
            ]
            if outside:
                raise AttemptBudgetError(
                    "successful extract URLs must belong to the submitted set"
                )
            if row["extract_usage_json"] is not None:
                if (
                    row["extract_usage_json"] == usage_json
                    and row["extract_success_urls_json"] == success_urls_json
                ):
                    return normalized_success_urls
                raise AttemptBudgetError("extract audit is already completed")
            connection.execute(
                """
                UPDATE attempts
                SET extract_success_urls_json = ?, extract_usage_json = ?
                WHERE attempt_id = ? AND extract_usage_json IS NULL
                """,
                (success_urls_json, usage_json, row["attempt_id"]),
            )
        return normalized_success_urls

    def allowed_evidence_urls(
        self,
        lease_token: str,
        *,
        now: datetime | None = None,
    ) -> list[str]:
        """Return only this active lease's successfully completed extract set."""

        timestamp = _utc(now)
        with self._connection() as connection:
            row = self._active_attempt(connection, lease_token, timestamp)
        if (
            row["extract_usage_json"] is None
            or row["extract_success_urls_json"] is None
        ):
            return []
        return list(json.loads(row["extract_success_urls_json"]))

    def reclaim_expired_leases(self, *, now: datetime | None = None) -> int:
        """Close expired attempts and place their products in transient backoff."""

        timestamp = _utc(now)
        with self._write_transaction() as connection:
            return self._reclaim_expired(connection, timestamp)

    def _reclaim_expired(
        self, connection: sqlite3.Connection, timestamp: datetime
    ) -> int:
        now_text = _time_text(timestamp)
        rows = connection.execute(
            """
            SELECT product_id, lease_token, consecutive_failures
            FROM products
            WHERE status = 'leased' AND lease_until <= ?
            """,
            (now_text,),
        ).fetchall()
        for row in rows:
            failures = int(row["consecutive_failures"]) + 1
            next_run = timestamp + retry_delay("lease_expired", failures)
            connection.execute(
                """
                UPDATE attempts
                SET finished_at = ?, outcome = 'lease_expired',
                    error = 'worker lease expired before an outcome was recorded'
                WHERE lease_token = ? AND finished_at IS NULL
                """,
                (now_text, row["lease_token"]),
            )
            connection.execute(
                """
                UPDATE products
                SET status = 'backoff', next_run_at = ?,
                    consecutive_failures = ?,
                    lease_token = NULL, lease_owner = NULL, lease_until = NULL,
                    leased_source_hash = NULL, reschedule_requested = 0,
                    last_outcome = 'lease_expired',
                    last_error = 'worker lease expired', updated_at = ?
                WHERE product_id = ? AND status = 'leased' AND lease_token = ?
                """,
                (
                    _time_text(next_run),
                    failures,
                    now_text,
                    row["product_id"],
                    row["lease_token"],
                ),
            )
        return len(rows)

    def record_outcome(
        self,
        lease: Lease | str | None = None,
        outcome: str | None = None,
        *,
        product_id: Any | None = None,
        lease_token: str | None = None,
        payload: Any = None,
        wiki_path: str | None = None,
        error: str | None = None,
        details: Any = None,
        now: datetime | None = None,
    ) -> OutcomeResult:
        """Finish a lease, audit it, and schedule refresh or retry.

        ``synced`` and ``success`` are refreshed in 365 days. Content outcomes
        follow 30/90/180-day backoff; transient failures use 1/6/24 hours and a
        Wiki.js edit conflict uses 24 hours.
        """

        if lease is not None and lease_token is not None:
            raise ValueError("pass either lease or lease_token, not both")
        if isinstance(lease, Lease):
            token = lease.token
            if product_id is None:
                product_id = lease.product_id
        elif lease is not None:
            token = str(lease)
        elif lease_token is not None:
            token = str(lease_token)
        else:
            token = ""
        if not token:
            raise ValueError("lease token is required")
        if not isinstance(outcome, str) or not outcome.strip():
            raise ValueError("outcome must be a non-empty string")
        normalised_outcome = outcome.strip().lower()
        success = normalised_outcome in {"synced", "success"}
        stored_outcome = "synced" if success else normalised_outcome
        timestamp = _utc(now)
        now_text = _time_text(timestamp)

        # Commit lease-expiry recovery separately so a subsequent LeaseLostError
        # cannot roll the recovery audit and transient backoff back.
        self.reclaim_expired_leases(now=timestamp)
        with self._write_transaction() as connection:
            row = connection.execute(
                """
                SELECT p.*, a.attempt_id
                FROM products AS p
                JOIN attempts AS a ON a.lease_token = p.lease_token
                WHERE p.status = 'leased' AND p.lease_token = ?
                  AND p.lease_until > ?
                  AND a.finished_at IS NULL
                """,
                (token, now_text),
            ).fetchone()
            if row is None:
                raise LeaseLostError("lease is missing, expired, or already completed")
            if product_id is not None and str(product_id) != row["product_id"]:
                raise LeaseLostError("product_id does not match the active lease")
            if isinstance(lease, Lease) and (
                lease.product_id != row["product_id"]
                or lease.source_hash != row["leased_source_hash"]
            ):
                raise LeaseLostError("lease identity does not match durable state")

            if row["reschedule_requested"] or row["source_hash"] != row["leased_source_hash"]:
                stale_details = {
                    "requested_outcome": normalised_outcome,
                    "details": _normalise_json(details),
                    "payload": _normalise_json(payload),
                    "wiki_path": wiki_path,
                }
                connection.execute(
                    """
                    UPDATE attempts
                    SET finished_at = ?, outcome = 'stale_source', error = ?,
                        details_json = ?
                    WHERE attempt_id = ?
                    """,
                    (
                        now_text,
                        error,
                        _canonical_json(stale_details),
                        row["attempt_id"],
                    ),
                )
                connection.execute(
                    """
                    UPDATE products
                    SET status = 'due', next_run_at = ?,
                        lease_token = NULL, lease_owner = NULL, lease_until = NULL,
                        leased_source_hash = NULL, reschedule_requested = 0,
                        last_outcome = 'stale_source', last_error = NULL,
                        updated_at = ?
                    WHERE product_id = ? AND lease_token = ?
                    """,
                    (now_text, now_text, row["product_id"], token),
                )
                return OutcomeResult(
                    row["product_id"],
                    "stale_source",
                    False,
                    "due",
                    timestamp,
                    int(row["consecutive_failures"]),
                )

            if success:
                failures = 0
                status = "synced"
                next_run = timestamp + timedelta(days=SYNC_REFRESH_DAYS)
                last_error = None
                last_success = now_text
            else:
                failures = int(row["consecutive_failures"]) + 1
                status = "backoff"
                next_run = timestamp + retry_delay(stored_outcome, failures)
                last_error = error
                last_success = row["last_success_at"]

            audit_details = details
            if payload is not None or wiki_path is not None:
                audit_details = {
                    "details": _normalise_json(details),
                    "payload": _normalise_json(payload),
                    "wiki_path": wiki_path,
                }
            details_json = (
                None if audit_details is None else _canonical_json(audit_details)
            )
            connection.execute(
                """
                UPDATE attempts
                SET finished_at = ?, outcome = ?, error = ?, details_json = ?
                WHERE attempt_id = ?
                """,
                (
                    now_text,
                    stored_outcome,
                    error,
                    details_json,
                    row["attempt_id"],
                ),
            )
            connection.execute(
                """
                UPDATE products
                SET status = ?, next_run_at = ?, consecutive_failures = ?,
                    lease_token = NULL, lease_owner = NULL, lease_until = NULL,
                    leased_source_hash = NULL, reschedule_requested = 0,
                    last_success_at = ?, last_outcome = ?, last_error = ?,
                    updated_at = ?
                WHERE product_id = ? AND lease_token = ?
                """,
                (
                    status,
                    _time_text(next_run),
                    failures,
                    last_success,
                    stored_outcome,
                    last_error,
                    now_text,
                    row["product_id"],
                    token,
                ),
            )

        return OutcomeResult(
            row["product_id"],
            stored_outcome,
            True,
            status,
            next_run,
            failures,
        )

    def attempt_history(self, product_id: Any | None = None) -> list[AttemptRecord]:
        """Return immutable attempt audit records in creation order."""

        with self._connection() as connection:
            if product_id is None:
                rows = connection.execute(
                    "SELECT * FROM attempts ORDER BY attempt_id"
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM attempts
                    WHERE product_id = ? ORDER BY attempt_id
                    """,
                    (str(product_id),),
                ).fetchall()
        return [self._attempt_record(row) for row in rows]

    def status_counts(
        self,
        *,
        now: datetime | None = None,
        include_zero: bool = True,
    ) -> dict[str, int]:
        """Count products by status, reclaiming expired leases first."""

        self.reclaim_expired_leases(now=now)
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM products GROUP BY status"
            ).fetchall()
        counts = {row["status"]: int(row["count"]) for row in rows}
        if include_zero:
            return {status: counts.get(status, 0) for status in PRODUCT_STATUSES}
        return counts

    def published_products(
        self,
        *,
        limit: int | None = None,
    ) -> list[PublishedProduct]:
        """Return one latest successful Wiki publication per product.

        ``products.status`` is deliberately not used: a page remains published
        when a changed catalogue row becomes due again or a later refresh is
        waiting for retry.  ``last_success_at`` and the latest successful
        attempt therefore define the reader-visible catalogue.
        """

        if limit is not None and (
            isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0
        ):
            raise ValueError("limit must be a positive integer or None")
        query = """
            SELECT
                p.product_id,
                p.payload_json,
                p.last_success_at,
                a.details_json
            FROM products AS p
            JOIN attempts AS a
              ON a.attempt_id = (
                  SELECT latest.attempt_id
                  FROM attempts AS latest
                  WHERE latest.product_id = p.product_id
                    AND latest.outcome = 'synced'
                    AND latest.finished_at IS NOT NULL
                  ORDER BY latest.attempt_id DESC
                  LIMIT 1
              )
            WHERE p.last_success_at IS NOT NULL
            ORDER BY p.last_success_at DESC, p.product_id
        """
        parameters: tuple[Any, ...] = ()
        if limit is not None:
            query += "\nLIMIT ?"
            parameters = (limit,)

        with self._connection() as connection:
            rows = connection.execute(query, parameters).fetchall()

        published: list[PublishedProduct] = []
        for row in rows:
            payload_value = json.loads(row["payload_json"])
            payload = dict(payload_value) if isinstance(payload_value, Mapping) else {}
            details_value = (
                json.loads(row["details_json"])
                if row["details_json"] is not None
                else {}
            )
            details = details_value if isinstance(details_value, Mapping) else {}
            recorded_payload = details.get("payload")
            decision_value = (
                recorded_payload.get("decision")
                if isinstance(recorded_payload, Mapping)
                else None
            )
            decision = (
                dict(decision_value) if isinstance(decision_value, Mapping) else {}
            )
            wiki_path_value = details.get("wiki_path")
            wiki_path = (
                wiki_path_value.strip()
                if isinstance(wiki_path_value, str) and wiki_path_value.strip()
                else None
            )
            published_at = _parse_time(row["last_success_at"])
            if published_at is None:  # guarded by SQL; retain a fail-closed boundary
                continue
            published.append(
                PublishedProduct(
                    product_id=row["product_id"],
                    payload=payload,
                    decision=decision,
                    wiki_path=wiki_path,
                    published_at=published_at,
                )
            )
        return published

    @staticmethod
    def _product_state(row: sqlite3.Row) -> ProductState:
        return ProductState(
            product_id=row["product_id"],
            source_hash=row["source_hash"],
            source_updated_at=_parse_time(row["source_updated_at"]),
            payload=json.loads(row["payload_json"]),
            status=row["status"],
            next_run_at=_parse_time(row["next_run_at"]),  # type: ignore[arg-type]
            consecutive_failures=int(row["consecutive_failures"]),
            lease_token=row["lease_token"],
            lease_owner=row["lease_owner"],
            lease_until=_parse_time(row["lease_until"]),
            leased_source_hash=row["leased_source_hash"],
            reschedule_requested=bool(row["reschedule_requested"]),
            last_attempt_at=_parse_time(row["last_attempt_at"]),
            last_success_at=_parse_time(row["last_success_at"]),
            last_outcome=row["last_outcome"],
            last_error=row["last_error"],
        )

    @staticmethod
    def _attempt_record(row: sqlite3.Row) -> AttemptRecord:
        return AttemptRecord(
            attempt_id=int(row["attempt_id"]),
            product_id=row["product_id"],
            source_hash=row["source_hash"],
            lease_token=row["lease_token"],
            worker_id=row["worker_id"],
            started_at=_parse_time(row["started_at"]),  # type: ignore[arg-type]
            lease_until=_parse_time(row["lease_until"]),  # type: ignore[arg-type]
            finished_at=_parse_time(row["finished_at"]),
            outcome=row["outcome"],
            error=row["error"],
            details=(
                json.loads(row["details_json"])
                if row["details_json"] is not None
                else None
            ),
            payload=json.loads(row["payload_json"]),
            search_started_at=_parse_time(row["search_started_at"]),
            search_urls=(
                json.loads(row["search_urls_json"])
                if row["search_urls_json"] is not None
                else None
            ),
            search_usage=(
                json.loads(row["search_usage_json"])
                if row["search_usage_json"] is not None
                else None
            ),
            extract_started_at=_parse_time(row["extract_started_at"]),
            extract_urls=(
                json.loads(row["extract_urls_json"])
                if row["extract_urls_json"] is not None
                else None
            ),
            extract_success_urls=(
                json.loads(row["extract_success_urls_json"])
                if row["extract_success_urls_json"] is not None
                else None
            ),
            extract_usage=(
                json.loads(row["extract_usage_json"])
                if row["extract_usage_json"] is not None
                else None
            ),
        )
