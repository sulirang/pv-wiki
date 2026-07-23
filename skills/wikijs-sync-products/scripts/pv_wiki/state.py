"""Durable, idempotent scheduler state for product wiki synchronisation.

SQLite owns only orchestration state; the PostgreSQL catalogue remains the
source of truth.  Every mutating scheduler operation uses ``BEGIN IMMEDIATE``
to prevent duplicate product claims at the lease layer. The supported HTTP
deployment remains one worker process because publication fences are
process-local.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
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


SCHEMA_VERSION = 7
BACKOFF_DAYS = (30, 90, 180)
TRANSIENT_BACKOFF_HOURS = (1, 6, 24)
INVALID_DECISION_BACKOFF_HOURS = (24, 72, 168, 720)
SOURCE_VERIFICATION_BACKOFF_DAYS = (7, 30, 90, 365)
RESEARCH_ACTION_STATUSES = (
    "started",
    "completed",
    "failed",
    "uncertain",
)
RESEARCH_ACTIONS = frozenset({"search", "extract", "ai"})
MAX_RESEARCH_ACTION_NAME_LENGTH = 64
MAX_RESEARCH_RESULT_SUMMARY_BYTES = 32 * 1024
MAX_RESEARCH_ACTION_ERROR_LENGTH = 1000
MAX_RESEARCH_ROUNDS = 3
MAX_RESEARCH_TOTAL_QUERIES = 7
MAX_RESEARCH_TOTAL_URLS = 5
MAX_RESEARCH_TOTAL_CREDITS = 100.0
MAX_LEGACY_SEARCH_QUERIES = 3
_RESEARCH_ACTION_PATTERN = re.compile(r"^[a-z][a-z0-9_.:-]{0,63}$")
_RESEARCH_SUMMARY_BODY_KEYS = frozenset(
    {
        "body",
        "content",
        "document",
        "evidence_text",
        "html",
        "markdown",
        "prompt",
        "quote",
        "raw_content",
        "response",
        "text",
    }
)
_RESEARCH_SUMMARY_BODY_KEYS_COLLAPSED = frozenset(
    key.replace("_", "") for key in _RESEARCH_SUMMARY_BODY_KEYS
)
_RESEARCH_AI_ACTION_TYPES = frozenset({"final", "search_more"})
_RESEARCH_AI_GAPS = frozenset(
    {
        "manufacturer_identity",
        "primary_datasheet",
        "independent_corroboration",
        "missing_exact_fact",
        "conflict_resolution",
        "scope_classification",
    }
)
_RESEARCH_AI_OUTCOMES = frozenset(
    {
        "",
        "publish",
        "no_datasheet",
        "ambiguous",
        "insufficient_identity",
        "out_of_scope",
    }
)
TRANSIENT_OUTCOMES = frozenset(
    {
        "tavily_error",
        "ai_error",
        "wikijs_error",
        "error",
        "lease_expired",
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

_CREATE_RESEARCH_ACTION_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS research_actions (
        action_id INTEGER PRIMARY KEY AUTOINCREMENT,
        attempt_id INTEGER NOT NULL
            REFERENCES attempts(attempt_id) ON DELETE CASCADE,
        round_number INTEGER NOT NULL CHECK (round_number >= 0),
        action TEXT NOT NULL
            CHECK (length(action) BETWEEN 1 AND 64),
        status TEXT NOT NULL
            CHECK (status IN ('started', 'completed', 'failed', 'uncertain')),
        request_fingerprint TEXT NOT NULL
            CHECK (
                length(request_fingerprint) = 64
                AND request_fingerprint NOT GLOB '*[^0-9a-f]*'
            ),
        started_at TEXT NOT NULL,
        finished_at TEXT,
        result_summary_json TEXT,
        credits REAL
            CHECK (credits IS NULL OR credits >= 0),
        error TEXT,
        CHECK (
            (status = 'started' AND finished_at IS NULL)
            OR
            (status <> 'started' AND finished_at IS NOT NULL)
        ),
        UNIQUE (attempt_id, round_number, action),
        UNIQUE (attempt_id, action, request_fingerprint)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS research_actions_attempt_idx
        ON research_actions(attempt_id, round_number, action)
    """,
    """
    CREATE INDEX IF NOT EXISTS research_actions_status_idx
        ON research_actions(status, action)
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
class ResearchActionRecord:
    """One at-most-once paid or externally visible research action."""

    action_id: int
    attempt_id: int
    product_id: str
    round_number: int
    action: str
    status: str
    request_fingerprint: str
    scope_fingerprint: str | None
    started_at: datetime
    finished_at: datetime | None
    result_summary: Any
    credits: float | None
    error: str | None


@dataclass(frozen=True, slots=True)
class ResearchActionStartResult:
    """Return an action record and whether the caller may execute it."""

    record: ResearchActionRecord
    should_execute: bool


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


def next_month_start(value: datetime | None = None) -> datetime:
    """Return the first instant of the next UTC calendar month."""

    current = _utc(value)
    if current.month == 12:
        return datetime(current.year + 1, 1, 1, tzinfo=timezone.utc)
    return datetime(current.year, current.month + 1, 1, tzinfo=timezone.utc)


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


def _research_action_name(value: Any) -> str:
    if not isinstance(value, str):
        raise TypeError("research action must be a string")
    action = value.strip().casefold()
    if not _RESEARCH_ACTION_PATTERN.fullmatch(action):
        raise ValueError(
            "research action must be a lowercase identifier of at most "
            f"{MAX_RESEARCH_ACTION_NAME_LENGTH} characters"
        )
    if action not in RESEARCH_ACTIONS:
        raise ValueError(
            "research action must be one of: ai, extract, search"
        )
    return action


def _research_round_number(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("round_number must be an integer")
    if not 0 <= value < MAX_RESEARCH_ROUNDS:
        raise ValueError(
            f"round_number must be between 0 and {MAX_RESEARCH_ROUNDS - 1}"
        )
    return value


def _research_request_fingerprint(value: Any) -> str:
    if not isinstance(value, str):
        raise TypeError("request_fingerprint must be a string")
    fingerprint = value.strip().casefold()
    if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        raise ValueError("request_fingerprint must be a SHA-256 hex digest")
    return fingerprint


def research_request_fingerprint(action: str, request: Any) -> str:
    """Hash a paid action request without persisting its potentially sensitive body."""

    normalized_action = _research_action_name(action)
    payload = _canonical_json(
        {
            "action": normalized_action,
            "request": request,
        }
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _research_credits(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise TypeError("credits must be a number")
    credits = float(value)
    if not math.isfinite(credits) or credits < 0:
        raise ValueError("credits must be finite and non-negative")
    return credits


def _research_error(value: Any, *, required: bool) -> str | None:
    if value is None:
        if required:
            raise ValueError("failed or uncertain research actions need an error")
        return None
    if not isinstance(value, str):
        raise TypeError("research action error must be a string")
    error = " ".join(value.split())
    if required and not error:
        raise ValueError("failed or uncertain research actions need an error")
    if len(error) > MAX_RESEARCH_ACTION_ERROR_LENGTH:
        raise ValueError(
            "research action error exceeds "
            f"{MAX_RESEARCH_ACTION_ERROR_LENGTH} characters"
        )
    return error or None


def _reject_research_bodies(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            separated_key = re.sub(
                r"(?<=[a-z0-9])(?=[A-Z])",
                "_",
                str(key),
            )
            normalized_key = re.sub(
                r"[^a-z0-9]+",
                "_",
                separated_key.casefold(),
            ).strip("_")
            collapsed_key = normalized_key.replace("_", "")
            if (
                normalized_key in _RESEARCH_SUMMARY_BODY_KEYS
                or collapsed_key in _RESEARCH_SUMMARY_BODY_KEYS_COLLAPSED
                or any(
                    token in _RESEARCH_SUMMARY_BODY_KEYS
                    or token.replace("_", "")
                    in _RESEARCH_SUMMARY_BODY_KEYS_COLLAPSED
                    for token in normalized_key.split("_")
                )
            ):
                raise ValueError(
                    "research result summaries cannot persist response bodies "
                    f"or evidence text ({key!r})"
                )
            _reject_research_bodies(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_research_bodies(item)


def _bounded_summary_strings(
    value: Any,
    *,
    name: str,
    maximum: int,
    item_limit: int,
) -> list[str]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise TypeError(f"{name} must be an array of strings")
    if len(value) > maximum:
        raise ValueError(f"{name} may contain at most {maximum} items")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise TypeError(f"{name} must be an array of strings")
        normalized = " ".join(item.split())
        if not normalized or len(normalized) > item_limit:
            raise ValueError(
                f"{name} items must contain 1-{item_limit} characters"
            )
        result.append(normalized)
    return result


def _bounded_summary_text(
    value: Any,
    *,
    name: str,
    maximum: int,
    allowed: frozenset[str] | None = None,
) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    normalized = " ".join(value.split())
    if len(normalized) > maximum:
        raise ValueError(f"{name} must contain at most {maximum} characters")
    if allowed is not None and normalized not in allowed:
        raise ValueError(f"{name} contains an unsupported value")
    return normalized


def _bounded_summary_count(value: Any, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if not 0 <= value <= 100:
        raise ValueError(f"{name} must be between 0 and 100")
    return value


def _bounded_summary_credits(value: Any, *, name: str) -> int | float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or not 0 <= float(value) <= MAX_RESEARCH_TOTAL_CREDITS
    ):
        raise ValueError(
            f"{name} must be a finite number between 0 and "
            f"{MAX_RESEARCH_TOTAL_CREDITS:g}"
        )
    number = float(value)
    return int(number) if number.is_integer() else number


def _research_result_summary_json(
    action: str,
    value: Any,
    *,
    require_successful_urls: bool = False,
) -> str | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise TypeError("research result_summary must be a mapping")
    summary = dict(value)
    _reject_research_bodies(summary)
    allowed_fields = {
        "search": frozenset(
            {
                "queries",
                "candidate_urls",
                "request_ids",
                "provider_requests",
                "completed_provider_requests",
                "known_partial_credits",
            }
        ),
        "extract": frozenset(
            {
                "submitted_urls",
                "successful_urls",
                "provider_requests",
                "completed_provider_requests",
                "known_partial_credits",
            }
        ),
        "ai": frozenset(
            {
                "action_type",
                "gap",
                "queries",
                "outcome",
                "manufacturer",
                "provider_requests",
                "provider_fingerprint",
                "http_status",
                "error_type",
            }
        ),
    }[action]
    unsupported = set(summary) - allowed_fields
    if unsupported:
        raise ValueError(
            "research result_summary contains unsupported fields: "
            + ", ".join(sorted(str(field) for field in unsupported))
        )
    if "queries" in summary:
        summary["queries"] = _bounded_summary_strings(
            summary["queries"],
            name="queries",
            maximum=3,
            item_limit=400,
        )
    if "request_ids" in summary:
        summary["request_ids"] = _bounded_summary_strings(
            summary["request_ids"],
            name="request_ids",
            maximum=20,
            item_limit=200,
        )
    if "candidate_urls" in summary:
        summary["candidate_urls"] = _canonical_urls(
            summary["candidate_urls"],
            maximum=20,
        )
    if "submitted_urls" in summary:
        summary["submitted_urls"] = _canonical_urls(
            summary["submitted_urls"],
            maximum=5,
            require_nonempty=True,
        )
    if "successful_urls" in summary:
        summary["successful_urls"] = _canonical_urls(
            summary["successful_urls"],
            maximum=5,
        )
    for field in ("provider_requests", "completed_provider_requests"):
        if field in summary:
            summary[field] = _bounded_summary_count(
                summary[field],
                name=field,
            )
    if "known_partial_credits" in summary:
        summary["known_partial_credits"] = _bounded_summary_credits(
            summary["known_partial_credits"],
            name="known_partial_credits",
        )
    if "action_type" in summary:
        summary["action_type"] = _bounded_summary_text(
            summary["action_type"],
            name="action_type",
            maximum=20,
            allowed=_RESEARCH_AI_ACTION_TYPES,
        )
    if "gap" in summary:
        summary["gap"] = _bounded_summary_text(
            summary["gap"],
            name="gap",
            maximum=50,
            allowed=_RESEARCH_AI_GAPS,
        )
    if "outcome" in summary:
        summary["outcome"] = _bounded_summary_text(
            summary["outcome"],
            name="outcome",
            maximum=100,
            allowed=_RESEARCH_AI_OUTCOMES,
        )
    if "manufacturer" in summary:
        summary["manufacturer"] = _bounded_summary_text(
            summary["manufacturer"],
            name="manufacturer",
            maximum=300,
        )
    if "provider_fingerprint" in summary:
        summary["provider_fingerprint"] = _research_request_fingerprint(
            summary["provider_fingerprint"]
        )
    if "http_status" in summary:
        status = summary["http_status"]
        if (
            isinstance(status, bool)
            or not isinstance(status, int)
            or not 100 <= status <= 599
        ):
            raise ValueError("http_status must be between 100 and 599")
    if "error_type" in summary:
        error_type = summary["error_type"]
        if (
            not isinstance(error_type, str)
            or re.fullmatch(
                r"[A-Za-z][A-Za-z0-9_]{0,99}",
                error_type,
            )
            is None
        ):
            raise ValueError("error_type must be a bounded class name")
    if (
        action == "extract"
        and require_successful_urls
        and "successful_urls" not in summary
    ):
        raise ValueError(
            "extract result_summary must include successful_urls"
        )
    if (
        action == "extract"
        and require_successful_urls
        and "submitted_urls" not in summary
    ):
        raise ValueError(
            "extract result_summary must include submitted_urls"
        )
    serialized = _canonical_json(summary)
    if len(serialized.encode("utf-8")) > MAX_RESEARCH_RESULT_SUMMARY_BYTES:
        raise ValueError(
            "research result_summary exceeds "
            f"{MAX_RESEARCH_RESULT_SUMMARY_BYTES} bytes"
        )
    return serialized


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


def _audited_usage_credits(
    serialized: str | None,
    *,
    name: str,
) -> float:
    """Return known legacy credits or fail closed when mixing audit formats."""

    if serialized is None:
        return 0.0
    try:
        usage = json.loads(serialized)
    except (TypeError, json.JSONDecodeError) as exc:
        raise StateError(f"{name} usage audit is invalid") from exc
    if not isinstance(usage, Mapping) or "credits" not in usage:
        raise StateError(
            f"{name} usage audit has no finite non-negative credits"
        )
    try:
        return _research_credits(usage["credits"])
    except (TypeError, ValueError) as exc:
        raise StateError(
            f"{name} usage audit has no finite non-negative credits"
        ) from exc


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
    if outcome == "out_of_scope":
        return timedelta(days=SYNC_REFRESH_DAYS)
    if outcome == "source_unverified":
        index = min(
            consecutive_failures - 1,
            len(SOURCE_VERIFICATION_BACKOFF_DAYS) - 1,
        )
        return timedelta(days=SOURCE_VERIFICATION_BACKOFF_DAYS[index])
    if outcome == "invalid_decision":
        index = min(
            consecutive_failures - 1,
            len(INVALID_DECISION_BACKOFF_HOURS) - 1,
        )
        return timedelta(hours=INVALID_DECISION_BACKOFF_HOURS[index])
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
            if current < 5:
                # Multi-round research uses an append-only, at-most-once action
                # ledger. The two unique constraints prevent both reusing a
                # round/action slot and replaying the same paid request in a
                # later round of the same attempt.
                for statement in _CREATE_RESEARCH_ACTION_SCHEMA:
                    connection.execute(statement)
                connection.execute("PRAGMA user_version = 5")
                current = 5
            if current < 6:
                # Cross-attempt replay suppression follows the actual research
                # identity/provider configuration, not unrelated catalogue
                # metadata such as family_code or updated_at.
                connection.execute(
                    "ALTER TABLE research_actions "
                    "ADD COLUMN scope_fingerprint TEXT"
                )
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS research_actions_scope_idx
                    ON research_actions(scope_fingerprint, status)
                    """
                )
                connection.execute("PRAGMA user_version = 6")
                current = 6
            if current < 7:
                # A short-lived v6 migration populated legacy rows with the
                # catalogue source hash. That value is not a provider/request
                # scope and could incorrectly unlock an ambiguous paid call.
                # Restore those rows to the fail-closed unknown-scope marker.
                connection.execute(
                    """
                    UPDATE research_actions
                    SET scope_fingerprint = NULL
                    WHERE scope_fingerprint = (
                        SELECT attempts.source_hash
                        FROM attempts
                        WHERE attempts.attempt_id =
                              research_actions.attempt_id
                    )
                    """
                )
                connection.execute("PRAGMA user_version = 7")
                current = 7
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

    def resume_tavily_quota_waits(self, *, now: datetime | None = None) -> int:
        """Make quota-paused products due after a monthly reset or key change."""

        now_text = _time_text(_utc(now))
        with self._write_transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE products
                SET status = 'due', next_run_at = ?, updated_at = ?
                WHERE status = 'backoff'
                  AND last_outcome = 'tavily_quota_exhausted'
                  AND lease_token IS NULL
                """,
                (now_text, now_text),
            )
        return max(cursor.rowcount, 0)

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

    def begin_research_action(
        self,
        lease_or_token: Lease | str,
        *,
        round_number: int,
        action: str,
        request_fingerprint: str,
        scope_fingerprint: str | None = None,
        blocking_scope_fingerprints: Mapping[str, str] | None = None,
        now: datetime | None = None,
    ) -> ResearchActionStartResult:
        """Reserve one research action without authorizing unsafe exact replay.

        ``should_execute`` is true only for the transaction that inserted the
        durable ``started`` row. An exact retry returns the existing row with
        ``should_execute`` false. Before any new provider call, this also
        suppresses work when an earlier attempt for the same product has a
        started or uncertain research action under the same action/provider
        scope. A legacy unresolved row with no reliable scope blocks
        fail-closed. Completed requests can be repeated by a later attempt
        because response bodies are deliberately not persisted and autonomous
        crash recovery may need fresh evidence.
        """

        normalized_round = _research_round_number(round_number)
        normalized_action = _research_action_name(action)
        normalized_fingerprint = _research_request_fingerprint(
            request_fingerprint
        )
        timestamp = _utc(now)
        now_text = _time_text(timestamp)

        with self._write_transaction() as connection:
            attempt = self._active_attempt(
                connection,
                lease_or_token,
                timestamp,
            )
            if scope_fingerprint is None:
                # Callers predating provider-aware scopes cannot safely prove
                # that a later request differs. Persist an explicit unknown
                # marker so any unresolved legacy action blocks fail-closed.
                normalized_scope = None
            else:
                normalized_scope = _research_request_fingerprint(
                    scope_fingerprint
                )
            if blocking_scope_fingerprints is None:
                normalized_blocking_scopes = {
                    candidate: normalized_scope
                    for candidate in RESEARCH_ACTIONS
                }
            else:
                if set(blocking_scope_fingerprints) != set(RESEARCH_ACTIONS):
                    raise ValueError(
                        "blocking_scope_fingerprints must contain exactly "
                        "ai, extract, and search"
                    )
                normalized_blocking_scopes = {
                    _research_action_name(candidate):
                    _research_request_fingerprint(candidate_scope)
                    for candidate, candidate_scope
                    in blocking_scope_fingerprints.items()
                }
            existing = connection.execute(
                """
                SELECT ra.*, a.product_id
                FROM research_actions AS ra
                JOIN attempts AS a ON a.attempt_id = ra.attempt_id
                WHERE ra.attempt_id = ?
                  AND ra.round_number = ?
                  AND ra.action = ?
                """,
                (
                    attempt["attempt_id"],
                    normalized_round,
                    normalized_action,
                ),
            ).fetchone()
            if existing is not None:
                if existing["request_fingerprint"] != normalized_fingerprint:
                    raise AttemptBudgetError(
                        "research round/action slot already contains a "
                        "different request"
                    )
                return ResearchActionStartResult(
                    self._research_action_record(existing),
                    False,
                )

            duplicate = connection.execute(
                """
                SELECT round_number
                FROM research_actions
                WHERE attempt_id = ?
                  AND action = ?
                  AND request_fingerprint = ?
                """,
                (
                    attempt["attempt_id"],
                    normalized_action,
                    normalized_fingerprint,
                ),
            ).fetchone()
            if duplicate is not None:
                raise AttemptBudgetError(
                    "the same paid research request is already recorded in "
                    f"round {int(duplicate['round_number'])}"
                )

            prior_rows = connection.execute(
                """
                SELECT ra.*, a.product_id
                FROM research_actions AS ra
                JOIN attempts AS a ON a.attempt_id = ra.attempt_id
                WHERE a.product_id = ?
                  AND a.attempt_id <> ?
                  AND ra.status IN ('started', 'uncertain')
                ORDER BY ra.action_id DESC
                """,
                (
                    attempt["product_id"],
                    attempt["attempt_id"],
                ),
            ).fetchall()
            for prior in prior_rows:
                prior_action = str(prior["action"])
                prior_scope = prior["scope_fingerprint"]
                blocks = (
                    prior_scope is None
                    or prior_action not in normalized_blocking_scopes
                    or normalized_blocking_scopes[prior_action] is None
                )
                if not blocks:
                    try:
                        normalized_prior_scope = (
                            _research_request_fingerprint(prior_scope)
                        )
                    except (TypeError, ValueError):
                        blocks = True
                    else:
                        blocks = (
                            normalized_prior_scope
                            == normalized_blocking_scopes[prior_action]
                        )
                if blocks:
                    return ResearchActionStartResult(
                        self._research_action_record(prior),
                        False,
                    )

            inserted = connection.execute(
                """
                INSERT INTO research_actions (
                    attempt_id, round_number, action, status,
                    request_fingerprint, scope_fingerprint, started_at
                ) VALUES (?, ?, ?, 'started', ?, ?, ?)
                """,
                (
                    attempt["attempt_id"],
                    normalized_round,
                    normalized_action,
                    normalized_fingerprint,
                    normalized_scope,
                    now_text,
                ),
            )
            row = connection.execute(
                """
                SELECT ra.*, a.product_id
                FROM research_actions AS ra
                JOIN attempts AS a ON a.attempt_id = ra.attempt_id
                WHERE ra.action_id = ?
                """,
                (inserted.lastrowid,),
            ).fetchone()
        if row is None:  # defensive: INSERT and SELECT share one transaction
            raise StateError("research action disappeared after insertion")
        return ResearchActionStartResult(
            self._research_action_record(row),
            True,
        )

    @staticmethod
    def _known_candidate_urls(
        connection: sqlite3.Connection,
        attempt_id: int,
    ) -> set[str]:
        row = connection.execute(
            """
            SELECT search_urls_json
            FROM attempts
            WHERE attempt_id = ?
            """,
            (attempt_id,),
        ).fetchone()
        candidates: list[str] = []
        if row is not None and row["search_urls_json"] is not None:
            try:
                candidates.extend(json.loads(row["search_urls_json"]))
            except (TypeError, json.JSONDecodeError) as exc:
                raise StateError("legacy search URL audit is invalid") from exc
        rows = connection.execute(
            """
            SELECT result_summary_json
            FROM research_actions
            WHERE attempt_id = ?
              AND action = 'search'
              AND status = 'completed'
            ORDER BY action_id
            """,
            (attempt_id,),
        ).fetchall()
        for action_row in rows:
            if action_row["result_summary_json"] is None:
                continue
            try:
                summary = json.loads(action_row["result_summary_json"])
            except (TypeError, json.JSONDecodeError) as exc:
                raise StateError("research search summary is invalid") from exc
            if not isinstance(summary, Mapping):
                raise StateError("research search summary must be an object")
            urls = summary.get("candidate_urls", [])
            try:
                candidates.extend(
                    _canonical_urls(urls, maximum=20)
                )
            except (TypeError, ValueError) as exc:
                raise StateError(
                    "research search candidate URL audit is invalid"
                ) from exc
        try:
            return set(_canonical_urls(candidates))
        except (TypeError, ValueError) as exc:
            raise StateError("search candidate URL audit is invalid") from exc

    def finish_research_action(
        self,
        lease_or_token: Lease | str,
        *,
        round_number: int,
        action: str,
        request_fingerprint: str,
        status: str,
        result_summary: Mapping[str, Any] | None = None,
        credits: int | float | Decimal | None = None,
        error: str | None = None,
        now: datetime | None = None,
    ) -> ResearchActionRecord:
        """Seal a started research action as completed, failed, or uncertain."""

        normalized_round = _research_round_number(round_number)
        normalized_action = _research_action_name(action)
        normalized_fingerprint = _research_request_fingerprint(
            request_fingerprint
        )
        if not isinstance(status, str):
            raise TypeError("research action status must be a string")
        normalized_status = status.strip().casefold()
        if normalized_status not in {"completed", "failed", "uncertain"}:
            raise ValueError(
                "research action status must be completed, failed, or uncertain"
            )
        normalized_error = _research_error(
            error,
            required=normalized_status in {"failed", "uncertain"},
        )
        if normalized_status == "completed" and normalized_error is not None:
            raise ValueError("completed research actions cannot have an error")
        if normalized_status == "completed" and result_summary is None:
            raise ValueError(
                "completed research actions require a result_summary"
            )
        summary_json = _research_result_summary_json(
            normalized_action,
            result_summary,
            require_successful_urls=(
                normalized_action == "extract"
                and normalized_status == "completed"
            ),
        )
        if credits is None and normalized_status != "uncertain":
            raise ValueError(
                "completed or failed research actions require an explicit "
                "known credit count"
            )
        normalized_credits = (
            None if credits is None else _research_credits(credits)
        )
        timestamp = _utc(now)
        now_text = _time_text(timestamp)

        with self._write_transaction() as connection:
            attempt = self._active_attempt(
                connection,
                lease_or_token,
                timestamp,
            )
            row = connection.execute(
                """
                SELECT ra.*, a.product_id
                FROM research_actions AS ra
                JOIN attempts AS a ON a.attempt_id = ra.attempt_id
                WHERE ra.attempt_id = ?
                  AND ra.round_number = ?
                  AND ra.action = ?
                """,
                (
                    attempt["attempt_id"],
                    normalized_round,
                    normalized_action,
                ),
            ).fetchone()
            if row is None:
                raise AttemptBudgetError(
                    "research action must begin before it can finish"
                )
            if row["request_fingerprint"] != normalized_fingerprint:
                raise AttemptBudgetError(
                    "request fingerprint does not match the started "
                    "research action"
                )
            if row["status"] != "started":
                stored_credits = (
                    None
                    if row["credits"] is None
                    else float(row["credits"])
                )
                if (
                    row["status"] == normalized_status
                    and row["result_summary_json"] == summary_json
                    and stored_credits == normalized_credits
                    and row["error"] == normalized_error
                ):
                    return self._research_action_record(row)
                raise AttemptBudgetError(
                    "research action is already in a terminal state"
                )

            if normalized_credits is not None:
                credit_row = connection.execute(
                    """
                    SELECT COALESCE(SUM(credits), 0) AS credits
                    FROM research_actions
                    WHERE attempt_id = ? AND action_id <> ?
                    """,
                    (attempt["attempt_id"], row["action_id"]),
                ).fetchone()
                existing_credits = (
                    float(credit_row["credits"])
                    if credit_row is not None
                    else 0.0
                )
                legacy_audit = connection.execute(
                    """
                    SELECT search_usage_json, extract_usage_json
                    FROM attempts
                    WHERE attempt_id = ?
                    """,
                    (attempt["attempt_id"],),
                ).fetchone()
                if legacy_audit is None:
                    raise StateError("attempt usage audit disappeared")
                existing_credits += _audited_usage_credits(
                    legacy_audit["search_usage_json"],
                    name="legacy search",
                )
                existing_credits += _audited_usage_credits(
                    legacy_audit["extract_usage_json"],
                    name="legacy extract",
                )
                if (
                    existing_credits + normalized_credits
                    > MAX_RESEARCH_TOTAL_CREDITS
                ):
                    raise AttemptBudgetError(
                        "research action credits exceed the compiled "
                        f"{MAX_RESEARCH_TOTAL_CREDITS:g}-credit ceiling"
                    )

            if (
                normalized_action == "search"
                and normalized_status == "completed"
            ):
                summary = (
                    json.loads(summary_json)
                    if summary_json is not None
                    else {}
                )
                queries = summary.get("queries")
                if not isinstance(queries, list) or not queries:
                    raise AttemptBudgetError(
                        "completed search actions must record their queries"
                    )
                query_rows = connection.execute(
                    """
                    SELECT result_summary_json
                    FROM research_actions
                    WHERE attempt_id = ?
                      AND action = 'search'
                      AND status = 'completed'
                      AND action_id <> ?
                    """,
                    (attempt["attempt_id"], row["action_id"]),
                ).fetchall()
                legacy_query_row = connection.execute(
                    """
                    SELECT search_usage_json
                    FROM attempts
                    WHERE attempt_id = ?
                    """,
                    (attempt["attempt_id"],),
                ).fetchone()
                if legacy_query_row is None:
                    raise StateError("attempt search audit disappeared")
                # The legacy search endpoint always ran build_queries(), whose
                # compiled maximum and normal output are three queries.
                total_queries = len(queries) + (
                    MAX_LEGACY_SEARCH_QUERIES
                    if legacy_query_row["search_usage_json"] is not None
                    else 0
                )
                for query_row in query_rows:
                    previous_summary = json.loads(
                        query_row["result_summary_json"] or "{}"
                    )
                    previous_queries = previous_summary.get("queries", [])
                    if not isinstance(previous_queries, list):
                        raise StateError(
                            "completed search query audit is invalid"
                        )
                    total_queries += len(previous_queries)
                if total_queries > MAX_RESEARCH_TOTAL_QUERIES:
                    raise AttemptBudgetError(
                        "research search queries exceed the compiled "
                        f"{MAX_RESEARCH_TOTAL_QUERIES}-query ceiling"
                    )

            if (
                normalized_action == "extract"
                and normalized_status == "completed"
            ):
                summary = (
                    json.loads(summary_json)
                    if summary_json is not None
                    else {}
                )
                submitted_urls = set(summary.get("submitted_urls", []))
                successful_urls = set(summary.get("successful_urls", []))
                if not successful_urls <= submitted_urls:
                    raise AttemptBudgetError(
                        "successful extract URLs must belong to this action's "
                        "submitted set"
                    )
                submitted_rows = connection.execute(
                    """
                    SELECT result_summary_json
                    FROM research_actions
                    WHERE attempt_id = ?
                      AND action = 'extract'
                      AND status = 'completed'
                      AND action_id <> ?
                    """,
                    (attempt["attempt_id"], row["action_id"]),
                ).fetchall()
                legacy_extract_row = connection.execute(
                    """
                    SELECT extract_urls_json
                    FROM attempts
                    WHERE attempt_id = ?
                    """,
                    (attempt["attempt_id"],),
                ).fetchone()
                if legacy_extract_row is None:
                    raise StateError("attempt extract audit disappeared")
                all_submitted_urls = set(submitted_urls)
                if legacy_extract_row["extract_urls_json"] is not None:
                    try:
                        all_submitted_urls.update(
                            _canonical_urls(
                                json.loads(
                                    legacy_extract_row["extract_urls_json"]
                                ),
                                maximum=MAX_RESEARCH_TOTAL_URLS,
                            )
                        )
                    except (
                        TypeError,
                        ValueError,
                        json.JSONDecodeError,
                    ) as exc:
                        raise StateError(
                            "legacy extract URL audit is invalid"
                        ) from exc
                for submitted_row in submitted_rows:
                    previous_summary = json.loads(
                        submitted_row["result_summary_json"] or "{}"
                    )
                    previous_submitted = previous_summary.get(
                        "submitted_urls",
                        [],
                    )
                    if not isinstance(previous_submitted, list):
                        raise StateError(
                            "completed extract submitted URL audit is invalid"
                        )
                    all_submitted_urls.update(previous_submitted)
                if len(all_submitted_urls) > MAX_RESEARCH_TOTAL_URLS:
                    raise AttemptBudgetError(
                        "research extracts exceed the compiled "
                        f"{MAX_RESEARCH_TOTAL_URLS}-URL ceiling"
                    )
                outside = submitted_urls - self._known_candidate_urls(
                    connection,
                    int(attempt["attempt_id"]),
                )
                if outside:
                    raise AttemptBudgetError(
                        "submitted extract URLs must belong to a completed "
                        "search action"
                    )

            changed = connection.execute(
                """
                UPDATE research_actions
                SET status = ?, finished_at = ?, result_summary_json = ?,
                    credits = ?, error = ?
                WHERE action_id = ? AND status = 'started'
                """,
                (
                    normalized_status,
                    now_text,
                    summary_json,
                    normalized_credits,
                    normalized_error,
                    row["action_id"],
                ),
            )
            if changed.rowcount != 1:  # defensive under BEGIN IMMEDIATE
                raise AttemptBudgetError(
                    "research action is already in a terminal state"
                )
            finished = connection.execute(
                """
                SELECT ra.*, a.product_id
                FROM research_actions AS ra
                JOIN attempts AS a ON a.attempt_id = ra.attempt_id
                WHERE ra.action_id = ?
                """,
                (row["action_id"],),
            ).fetchone()
        if finished is None:  # defensive
            raise StateError("research action disappeared after completion")
        return self._research_action_record(finished)

    def get_research_action(
        self,
        attempt_id: int,
        round_number: int,
        action: str,
    ) -> ResearchActionRecord | None:
        """Return one action slot without requiring its lease to remain active."""

        normalized_attempt = self._research_attempt_id(attempt_id)
        normalized_round = _research_round_number(round_number)
        normalized_action = _research_action_name(action)
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT ra.*, a.product_id
                FROM research_actions AS ra
                JOIN attempts AS a ON a.attempt_id = ra.attempt_id
                WHERE ra.attempt_id = ?
                  AND ra.round_number = ?
                  AND ra.action = ?
                """,
                (
                    normalized_attempt,
                    normalized_round,
                    normalized_action,
                ),
            ).fetchone()
        return (
            self._research_action_record(row)
            if row is not None
            else None
        )

    @staticmethod
    def _research_attempt_id(value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError("attempt_id must be an integer")
        if value <= 0:
            raise ValueError("attempt_id must be positive")
        return value

    def research_action_history(
        self,
        *,
        attempt_id: int | None = None,
        product_id: Any | None = None,
        limit: int = 1000,
    ) -> list[ResearchActionRecord]:
        """Return bounded action-ledger rows in creation order."""

        clauses: list[str] = []
        parameters: list[Any] = []
        if attempt_id is not None:
            clauses.append("ra.attempt_id = ?")
            parameters.append(self._research_attempt_id(attempt_id))
        if product_id is not None:
            product_text = str(product_id).strip()
            if not product_text:
                raise ValueError("product_id must be non-empty")
            clauses.append("a.product_id = ?")
            parameters.append(product_text)
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise TypeError("limit must be an integer")
        if not 1 <= limit <= 10_000:
            raise ValueError("limit must be between 1 and 10000")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.append(limit)
        with self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT ra.*, a.product_id
                FROM research_actions AS ra
                JOIN attempts AS a ON a.attempt_id = ra.attempt_id
                {where}
                ORDER BY ra.action_id
                LIMIT ?
                """,
                parameters,
            ).fetchall()
        return [self._research_action_record(row) for row in rows]

    def recent_ai_provider_rejection(
        self,
        provider_fingerprint: str,
        *,
        http_statuses: Iterable[int],
        within: timedelta,
        now: datetime | None = None,
    ) -> int | None:
        """Return a recent provider-global AI rejection for this configuration."""

        normalized_fingerprint = _research_request_fingerprint(
            provider_fingerprint
        )
        if not isinstance(within, timedelta) or within <= timedelta(0):
            raise ValueError("within must be a positive timedelta")
        normalized_statuses = {
            int(status)
            for status in http_statuses
            if (
                not isinstance(status, bool)
                and isinstance(status, int)
                and 100 <= status <= 599
            )
        }
        if not normalized_statuses:
            raise ValueError("http_statuses must contain an HTTP status")
        cutoff = _time_text(_utc(now) - within)
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT result_summary_json
                FROM research_actions
                WHERE action = 'ai'
                  AND status IN ('failed', 'uncertain')
                  AND finished_at >= ?
                  AND result_summary_json IS NOT NULL
                ORDER BY action_id DESC
                LIMIT 1000
                """,
                (cutoff,),
            ).fetchall()
        for row in rows:
            try:
                summary = json.loads(row["result_summary_json"])
            except (TypeError, json.JSONDecodeError) as exc:
                raise StateError(
                    "AI provider rejection audit is invalid"
                ) from exc
            if not isinstance(summary, Mapping):
                raise StateError("AI provider rejection audit is invalid")
            status = summary.get("http_status")
            if (
                summary.get("provider_fingerprint")
                == normalized_fingerprint
                and isinstance(status, int)
                and not isinstance(status, bool)
                and status in normalized_statuses
            ):
                return status
        return None

    def recent_ai_provider_error_products(
        self,
        provider_fingerprint: str,
        *,
        error_types: Iterable[str],
        within: timedelta,
        now: datetime | None = None,
    ) -> int:
        """Count distinct products with a recent matching AI provider error."""

        normalized_fingerprint = _research_request_fingerprint(
            provider_fingerprint
        )
        if not isinstance(within, timedelta) or within <= timedelta(0):
            raise ValueError("within must be a positive timedelta")
        normalized_error_types = {
            value.strip()
            for value in error_types
            if isinstance(value, str)
            and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,99}", value.strip())
        }
        if not normalized_error_types:
            raise ValueError("error_types must contain a valid class name")
        cutoff = _time_text(_utc(now) - within)
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT a.product_id, ra.result_summary_json
                FROM research_actions AS ra
                JOIN attempts AS a ON a.attempt_id = ra.attempt_id
                WHERE ra.action = 'ai'
                  AND ra.status IN ('failed', 'uncertain')
                  AND ra.finished_at >= ?
                  AND ra.result_summary_json IS NOT NULL
                ORDER BY ra.action_id DESC
                LIMIT 1000
                """,
                (cutoff,),
            ).fetchall()
        products: set[str] = set()
        for row in rows:
            try:
                summary = json.loads(row["result_summary_json"])
            except (TypeError, json.JSONDecodeError) as exc:
                raise StateError("AI provider error audit is invalid") from exc
            if not isinstance(summary, Mapping):
                raise StateError("AI provider error audit is invalid")
            if (
                summary.get("provider_fingerprint")
                == normalized_fingerprint
                and summary.get("error_type") in normalized_error_types
            ):
                products.add(str(row["product_id"]))
        return len(products)

    def research_action_stats(
        self,
        *,
        attempt_id: int | None = None,
        product_id: Any | None = None,
    ) -> dict[str, Any]:
        """Return JSON-friendly action counts and known credit totals."""

        clauses: list[str] = []
        parameters: list[Any] = []
        if attempt_id is not None:
            clauses.append("ra.attempt_id = ?")
            parameters.append(self._research_attempt_id(attempt_id))
        if product_id is not None:
            product_text = str(product_id).strip()
            if not product_text:
                raise ValueError("product_id must be non-empty")
            clauses.append("a.product_id = ?")
            parameters.append(product_text)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        with self._connection() as connection:
            totals = connection.execute(
                f"""
                SELECT
                    COUNT(*) AS actions,
                    COALESCE(SUM(ra.credits), 0) AS known_credits,
                    SUM(
                        CASE
                            WHEN ra.credits IS NULL
                              OR ra.status = 'uncertain'
                            THEN 1
                            ELSE 0
                        END
                    )
                        AS unknown_credit_actions,
                    MAX(ra.round_number) AS max_round
                FROM research_actions AS ra
                JOIN attempts AS a ON a.attempt_id = ra.attempt_id
                {where}
                """,
                parameters,
            ).fetchone()
            status_rows = connection.execute(
                f"""
                SELECT ra.status, COUNT(*) AS actions
                FROM research_actions AS ra
                JOIN attempts AS a ON a.attempt_id = ra.attempt_id
                {where}
                GROUP BY ra.status
                ORDER BY ra.status
                """,
                parameters,
            ).fetchall()
            action_rows = connection.execute(
                f"""
                SELECT
                    ra.action,
                    COUNT(*) AS actions,
                    COALESCE(SUM(ra.credits), 0) AS credits
                FROM research_actions AS ra
                JOIN attempts AS a ON a.attempt_id = ra.attempt_id
                {where}
                GROUP BY ra.action
                ORDER BY ra.action
                """,
                parameters,
            ).fetchall()

        by_status = {status: 0 for status in RESEARCH_ACTION_STATUSES}
        for row in status_rows:
            by_status[str(row["status"])] = int(row["actions"])
        by_action = {
            str(row["action"]): {
                "actions": int(row["actions"]),
                "credits": float(row["credits"]),
            }
            for row in action_rows
        }
        if totals is None:  # aggregate SELECT always returns one row
            raise StateError("research action statistics query returned no row")
        return {
            "actions": int(totals["actions"]),
            "known_credits": float(totals["known_credits"]),
            "unknown_credit_actions": int(
                totals["unknown_credit_actions"] or 0
            ),
            "max_round": (
                int(totals["max_round"])
                if totals["max_round"] is not None
                else None
            ),
            "by_status": by_status,
            "by_action": by_action,
        }

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
            urls: list[str] = []
            if (
                row["extract_usage_json"] is not None
                and row["extract_success_urls_json"] is not None
            ):
                try:
                    urls.extend(json.loads(row["extract_success_urls_json"]))
                except (TypeError, json.JSONDecodeError) as exc:
                    raise StateError(
                        "legacy successful extract URL audit is invalid"
                    ) from exc
            actions = connection.execute(
                """
                SELECT result_summary_json
                FROM research_actions
                WHERE attempt_id = ?
                  AND action = 'extract'
                  AND status = 'completed'
                ORDER BY action_id
                """,
                (row["attempt_id"],),
            ).fetchall()
            for action_row in actions:
                if action_row["result_summary_json"] is None:
                    raise StateError(
                        "completed extract action has no result summary"
                    )
                try:
                    summary = json.loads(action_row["result_summary_json"])
                except (TypeError, json.JSONDecodeError) as exc:
                    raise StateError(
                        "research extract summary is invalid"
                    ) from exc
                if not isinstance(summary, Mapping):
                    raise StateError(
                        "research extract summary must be an object"
                    )
                successful_urls = summary.get("successful_urls")
                try:
                    urls.extend(
                        _canonical_urls(successful_urls, maximum=5)
                    )
                except (TypeError, ValueError) as exc:
                    raise StateError(
                        "research successful extract URL audit is invalid"
                    ) from exc
        try:
            return _canonical_urls(urls)
        except (TypeError, ValueError) as exc:
            raise StateError(
                "successful extract URL audit is invalid"
            ) from exc

    def reclaim_expired_leases(self, *, now: datetime | None = None) -> int:
        """Close expired attempts, preserving immediate source-change work."""

        timestamp = _utc(now)
        with self._write_transaction() as connection:
            return self._reclaim_expired(connection, timestamp)

    @staticmethod
    def _mark_started_research_actions_uncertain(
        connection: sqlite3.Connection,
        attempt_id: int,
        *,
        finished_at: str,
        error: str,
    ) -> int:
        changed = connection.execute(
            """
            UPDATE research_actions
            SET status = 'uncertain', finished_at = ?, error = ?
            WHERE attempt_id = ? AND status = 'started'
            """,
            (finished_at, error, attempt_id),
        )
        return max(changed.rowcount, 0)

    def _reclaim_expired(
        self, connection: sqlite3.Connection, timestamp: datetime
    ) -> int:
        now_text = _time_text(timestamp)
        rows = connection.execute(
            """
            SELECT
                p.product_id,
                p.lease_token,
                p.consecutive_failures,
                p.source_hash,
                p.leased_source_hash,
                p.reschedule_requested,
                a.attempt_id
            FROM products AS p
            JOIN attempts AS a ON a.lease_token = p.lease_token
            WHERE p.status = 'leased'
              AND p.lease_until <= ?
              AND a.finished_at IS NULL
            """,
            (now_text,),
        ).fetchall()
        for row in rows:
            source_changed = (
                bool(row["reschedule_requested"])
                or row["source_hash"] != row["leased_source_hash"]
            )
            if source_changed:
                attempt_outcome = "stale_source"
                attempt_error = (
                    "source changed before the expired lease completed"
                )
                failures = 0
                product_status = "due"
                next_run = timestamp
                product_error = None
            else:
                attempt_outcome = "lease_expired"
                attempt_error = (
                    "worker lease expired before an outcome was recorded"
                )
                failures = int(row["consecutive_failures"]) + 1
                product_status = "backoff"
                next_run = timestamp + retry_delay(
                    "lease_expired",
                    failures,
                )
                product_error = "worker lease expired"
            self._mark_started_research_actions_uncertain(
                connection,
                int(row["attempt_id"]),
                finished_at=now_text,
                error="lease expired before research action completion",
            )
            connection.execute(
                """
                UPDATE attempts
                SET finished_at = ?, outcome = ?, error = ?
                WHERE lease_token = ? AND finished_at IS NULL
                """,
                (
                    now_text,
                    attempt_outcome,
                    attempt_error,
                    row["lease_token"],
                ),
            )
            connection.execute(
                """
                UPDATE products
                SET status = ?, next_run_at = ?,
                    consecutive_failures = ?,
                    lease_token = NULL, lease_owner = NULL, lease_until = NULL,
                    leased_source_hash = NULL, reschedule_requested = 0,
                    last_outcome = ?, last_error = ?, updated_at = ?
                WHERE product_id = ? AND status = 'leased' AND lease_token = ?
                """,
                (
                    product_status,
                    _time_text(next_run),
                    failures,
                    attempt_outcome,
                    product_error,
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
        use outcome-specific automatic backoff; transient failures use
        1/6/24 hours and a Wiki.js edit conflict uses 24 hours.
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

            self._mark_started_research_actions_uncertain(
                connection,
                int(row["attempt_id"]),
                finished_at=now_text,
                error="attempt ended before research action completion",
            )

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
            elif stored_outcome == "tavily_quota_exhausted":
                failures = int(row["consecutive_failures"])
                status = "backoff"
                next_run = next_month_start(timestamp)
                last_error = error
                last_success = row["last_success_at"]
            else:
                failures = int(row["consecutive_failures"]) + 1
                previous_outcomes = connection.execute(
                    """
                    SELECT outcome
                    FROM attempts
                    WHERE product_id = ? AND finished_at IS NOT NULL
                    ORDER BY attempt_id DESC
                    LIMIT 32
                    """,
                    (row["product_id"],),
                ).fetchall()
                outcome_streak = 1
                for previous in previous_outcomes:
                    if previous["outcome"] != stored_outcome:
                        break
                    outcome_streak += 1
                status = "backoff"
                next_run = timestamp + retry_delay(
                    stored_outcome,
                    outcome_streak,
                )
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

    def outcome_counts(self) -> dict[str, int]:
        """Return aggregate finished-attempt counts without exposing evidence."""

        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT outcome, COUNT(*) AS count
                FROM attempts
                WHERE finished_at IS NOT NULL AND outcome IS NOT NULL
                GROUP BY outcome
                ORDER BY count DESC, outcome
                """
            ).fetchall()
        return {str(row["outcome"]): int(row["count"]) for row in rows}

    def recent_distinct_outcome_streak(
        self,
        outcome: str,
        *,
        limit: int,
        within: timedelta,
        now: datetime | None = None,
    ) -> int:
        """Count distinct products in the newest consecutive outcome streak."""

        if not isinstance(outcome, str) or not outcome.strip():
            raise ValueError("outcome must be a non-empty string")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("limit must be a positive integer")
        if not isinstance(within, timedelta) or within <= timedelta(0):
            raise ValueError("within must be a positive timedelta")
        cutoff = _utc(now) - within
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT product_id, outcome
                FROM attempts
                WHERE finished_at IS NOT NULL AND finished_at >= ?
                ORDER BY attempt_id DESC
                """,
                (_time_text(cutoff),),
            ).fetchall()
        expected = outcome.strip().casefold()
        product_ids: set[str] = set()
        for row in rows:
            if (
                not isinstance(row["outcome"], str)
                or row["outcome"].casefold() != expected
            ):
                break
            product_ids.add(str(row["product_id"]))
            if len(product_ids) >= limit:
                return limit
        return len(product_ids)

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

    @staticmethod
    def _research_action_record(row: sqlite3.Row) -> ResearchActionRecord:
        raw_scope = row["scope_fingerprint"]
        try:
            normalized_scope = (
                None
                if raw_scope is None
                else _research_request_fingerprint(raw_scope)
            )
        except (TypeError, ValueError):
            # Unknown legacy values are represented as an absent scope so
            # callers retain the same fail-closed replay behavior.
            normalized_scope = None
        return ResearchActionRecord(
            action_id=int(row["action_id"]),
            attempt_id=int(row["attempt_id"]),
            product_id=str(row["product_id"]),
            round_number=int(row["round_number"]),
            action=str(row["action"]),
            status=str(row["status"]),
            request_fingerprint=str(row["request_fingerprint"]),
            scope_fingerprint=normalized_scope,
            started_at=_parse_time(row["started_at"]),  # type: ignore[arg-type]
            finished_at=_parse_time(row["finished_at"]),
            result_summary=(
                json.loads(row["result_summary_json"])
                if row["result_summary_json"] is not None
                else None
            ),
            credits=(
                float(row["credits"])
                if row["credits"] is not None
                else None
            ),
            error=row["error"],
        )
