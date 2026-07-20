"""Read-only access to the product catalogue in PostgreSQL.

The module deliberately imports :mod:`psycopg` only when a connection is
opened.  A deployment can therefore use the state and rendering utilities
without installing the PostgreSQL driver.  libpq's standard ``PG*``
environment variables remain the source of connection configuration, while a
validated ``sslmode`` is passed explicitly so weaker service-file settings
cannot downgrade the transport.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any


PRODUCT_COLUMNS = (
    "product_id",
    "brand_code",
    "family_code",
    "product_name",
    "unit_of_measure",
    "created_at",
    "updated_at",
)

# All identifiers are constants.  In particular, neither a table nor a column
# name can be supplied by a caller.
_SELECT_ALL = """
SELECT
    product_id,
    brand_code,
    family_code,
    product_name,
    unit_of_measure,
    created_at,
    updated_at
FROM public.products
ORDER BY COALESCE(updated_at, created_at), product_id
""".strip()

_SELECT_SINCE = """
SELECT
    product_id,
    brand_code,
    family_code,
    product_name,
    unit_of_measure,
    created_at,
    updated_at
FROM public.products
WHERE COALESCE(updated_at, created_at) >= %s
ORDER BY COALESCE(updated_at, created_at), product_id
""".strip()

_READ_ONLY_TRANSACTION = "SET TRANSACTION READ ONLY"
_ALLOWED_SSLMODES = frozenset({"require", "verify-ca", "verify-full"})


class DatabaseConfigurationError(RuntimeError):
    """Raised when PostgreSQL connection settings violate the safety policy."""


class DatabaseDependencyError(RuntimeError):
    """Raised when the optional PostgreSQL driver is unavailable."""


def validate_postgres_sslmode() -> str:
    """Return the normalized, mandatory PostgreSQL TLS mode.

    The invalid value is deliberately omitted from errors because environment
    input should not be copied into cron logs.  ``require`` is the minimum;
    deployments with a verifiable CA and hostname should use ``verify-full``.
    """

    raw = os.getenv("PGSSLMODE")
    if raw is None or not raw.strip():
        raise DatabaseConfigurationError(
            "PGSSLMODE is required and must be require, verify-ca, or verify-full"
        )
    normalized = raw.strip().casefold()
    if normalized not in _ALLOWED_SSLMODES:
        raise DatabaseConfigurationError(
            "PGSSLMODE must be require, verify-ca, or verify-full; "
            "weaker and unknown modes are refused"
        )
    return normalized


@dataclass(frozen=True, slots=True)
class Product:
    """One row from ``public.products``.

    PostgreSQL identifiers are intentionally kept in their native Python type
    here (for example UUID or integer).  The state store canonicalises the
    stable key to text when persisting it in SQLite.
    """

    product_id: Any
    brand_code: str | None
    family_code: str | None
    product_name: str | None
    unit_of_measure: str | None
    created_at: datetime | None
    updated_at: datetime | None

    @classmethod
    def from_row(cls, row: Any) -> "Product":
        if isinstance(row, Mapping):
            values = [row[column] for column in PRODUCT_COLUMNS]
        else:
            values = list(row)
        if len(values) != len(PRODUCT_COLUMNS):
            raise ValueError(
                f"expected {len(PRODUCT_COLUMNS)} product columns, got {len(values)}"
            )
        return cls(*values)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_dict(self) -> dict[str, Any]:
        """Alias used by JSON-oriented orchestration code."""

        return self.as_dict()

    def asdict(self) -> dict[str, Any]:
        """Compatibility alias for callers that use dataclass terminology."""

        return self.as_dict()


def _connect_from_environment() -> Any:
    """Open a psycopg connection using libpq's standard environment rules."""

    sslmode = validate_postgres_sslmode()

    try:
        import psycopg  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - exact import path is env-specific
        raise DatabaseDependencyError(
            "PostgreSQL access requires psycopg 3; install it in the runtime "
            "and configure the connection with standard PG* environment variables"
        ) from exc

    # Passing no DSN makes libpq consult PGHOST, PGPORT, PGDATABASE, PGUSER,
    # PGPASSWORD, PGSERVICE, and the other standard variables.  The explicit
    # keyword prevents a service file from weakening the validated TLS mode.
    return psycopg.connect(sslmode=sslmode)


class ProductReader:
    """Stream products from the fixed, read-only catalogue query."""

    def __init__(self, connect_factory: Callable[[], Any] | None = None) -> None:
        self._connect_factory = connect_factory or _connect_from_environment

    def iter_products(
        self,
        *,
        since: datetime | None = None,
        batch_size: int = 500,
    ) -> Iterator[Product]:
        """Yield products, optionally including rows changed at or after ``since``.

        ``>=`` is deliberate: rows sharing the high-water timestamp may be
        observed again after a restart, while source hashes in the state store
        make that replay harmless.  This avoids losing rows when multiple
        updates have the same timestamp.
        """

        if isinstance(batch_size, bool) or not isinstance(batch_size, int):
            raise TypeError("batch_size must be an integer")
        if batch_size <= 0:
            raise ValueError("batch_size must be greater than zero")

        connection = self._connect_factory()
        cursor = None
        try:
            cursor = connection.cursor()
            # This is defence in depth.  Production credentials must still be
            # a PostgreSQL role with SELECT-only privileges on the view/table.
            cursor.execute(_READ_ONLY_TRANSACTION)
            if since is None:
                cursor.execute(_SELECT_ALL)
            else:
                cursor.execute(_SELECT_SINCE, (since,))

            while True:
                rows = cursor.fetchmany(batch_size)
                if not rows:
                    break
                for row in rows:
                    yield Product.from_row(row)
        finally:
            if cursor is not None:
                cursor.close()
            # End the read transaction without ever committing session state.
            rollback = getattr(connection, "rollback", None)
            if rollback is not None:
                rollback()
            connection.close()

    def fetch_products(
        self,
        *,
        since: datetime | None = None,
        batch_size: int = 500,
    ) -> list[Product]:
        """Materialise :meth:`iter_products`; useful for small catalogues/tests."""

        return list(self.iter_products(since=since, batch_size=batch_size))


def iter_products(
    *,
    since: datetime | None = None,
    batch_size: int = 500,
    connect_factory: Callable[[], Any] | None = None,
) -> Iterator[Product]:
    """Convenience wrapper around :class:`ProductReader`."""

    return ProductReader(connect_factory).iter_products(
        since=since,
        batch_size=batch_size,
    )


def fetch_products(
    *,
    since: datetime | None = None,
    batch_size: int = 500,
    connect_factory: Callable[[], Any] | None = None,
) -> list[Product]:
    """Convenience wrapper returning all selected products as a list."""

    return ProductReader(connect_factory).fetch_products(
        since=since,
        batch_size=batch_size,
    )
