"""Durable, idempotent scheduler state for product wiki synchronisation.

The production state backend is PostgreSQL; SQLite remains supported for local
tests and legacy recovery. Mutating operations are serialized with
``BEGIN IMMEDIATE`` on SQLite or a transaction-scoped PostgreSQL advisory lock
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
import threading
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

import fcntl
import psycopg
from psycopg.rows import dict_row


SCHEMA_VERSION = 13
_POSTGRES_ADVISORY_LOCK = 8_604_293_015
_POSTGRES_PUBLICATION_LOCK = 8_604_293_016
_MEMORY_PUBLICATION_LOCK = threading.Lock()
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
_RESEARCH_AI_ERROR_CATEGORIES = frozenset(
    {
        "empty_content",
        "invalid_json",
        "provider_envelope",
        "incomplete_response",
        "decision_contract",
        "action_contract",
        "search_query_contract",
    }
)
_RESEARCH_AI_USAGE_KEYS = frozenset(
    {
        "accepted_prediction_tokens",
        "cache_hit_tokens",
        "cache_miss_tokens",
        "cached_tokens",
        "completion_tokens",
        "prompt_cache_hit_tokens",
        "prompt_cache_miss_tokens",
        "prompt_tokens",
        "reasoning_tokens",
        "rejected_prediction_tokens",
        "total_tokens",
    }
)
MAX_RESEARCH_AI_TOKEN_USAGE = 1_000_000_000
TRANSIENT_OUTCOMES = frozenset(
    {
        "tavily_error",  # Legacy persisted value from pre-Exa workers.
        "search_error",
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
EFFECTIVE_PRODUCT_STATUSES = (*PRODUCT_STATUSES, "retired")
PAGE_RETIREMENT_BASES = (
    "synced_publication",
    "failed_publication",
    "legacy_managed_page",
)
PAGE_RETIREMENT_FAILURE_OUTCOMES = (
    "wikijs_error",
    "publish_error",
    "wikijs_conflict",
)
DUE_QUEUE_WEIGHT = 4
BACKOFF_QUEUE_WEIGHT = 1
CONTENT_FAILURE_OUTCOMES = frozenset(
    {
        "ambiguous",
        "insufficient_identity",
        "invalid_decision",
        "no_datasheet",
        "out_of_scope",
        "source_unverified",
    }
)
MAX_REQUEUE_OUTCOMES = 32
MAX_REQUEUE_REASON_LENGTH = 200
MAX_SYSTEM_PAUSE_REASON_LENGTH = 200
FACT_DIAGNOSTIC_REASON_CODES = frozenset(
    {
        "duplicate_name",
        "invalid_name",
        "limit_exceeded",
        "validation_failed",
    }
)
MAX_FACT_DIAGNOSTIC_COUNT = 10_000
PARAMETER_ANALYSIS_STATUSES = ("started", "completed", "failed")
MAX_VERIFIED_PARAMETER_COUNT = 10_000
MAX_VERIFIED_PARAMETERS_JSON_BYTES = 4 * 1024 * 1024
MAX_PARAMETER_ANALYSIS_JSON_BYTES = 512 * 1024
MAX_PARAMETER_ANALYSIS_USAGE_JSON_BYTES = 32 * 1024
MAX_PARAMETER_VERSION_LENGTH = 200
MAX_PARAMETER_ANALYSIS_MODEL_LENGTH = 300
MAX_PARAMETER_ANALYSIS_ERROR_LENGTH = 2_000
MAX_PAGE_RETIREMENT_BATCH = 10_000
MAX_PAGE_RETIREMENT_PATH_LENGTH = 1_000
MAX_PAGE_RETIREMENT_REASON_LENGTH = 200
MAX_PAGE_RETIREMENT_BACKUP_REFERENCE_LENGTH = 1_000

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

_CREATE_REQUEUE_EVENT_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS requeue_events (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        product_id TEXT NOT NULL
            REFERENCES products(product_id) ON DELETE CASCADE,
        cutoff_attempt_id INTEGER NOT NULL
            REFERENCES attempts(attempt_id),
        previous_status TEXT NOT NULL
            CHECK (previous_status IN ('backoff', 'synced')),
        previous_outcome TEXT NOT NULL,
        reason TEXT NOT NULL
            CHECK (length(reason) BETWEEN 1 AND 200),
        attempted_after TEXT,
        attempted_before TEXT,
        requeued_at TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS requeue_events_product_idx
        ON requeue_events(product_id, event_id)
    """,
)

_CREATE_CONTENT_REFRESH_EVENT_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS content_refresh_events (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        product_id TEXT NOT NULL
            REFERENCES products(product_id) ON DELETE CASCADE,
        source_attempt_id INTEGER NOT NULL
            REFERENCES attempts(attempt_id),
        previous_content_schema_version INTEGER NOT NULL
            CHECK (previous_content_schema_version >= 0),
        content_schema_version INTEGER NOT NULL
            CHECK (content_schema_version >= 1),
        wiki_action TEXT NOT NULL
            CHECK (wiki_action IN ('unchanged', 'updated')),
        fact_diagnostics_json TEXT NOT NULL,
        refreshed_at TEXT NOT NULL,
        UNIQUE (product_id, source_attempt_id, content_schema_version)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS content_refresh_events_product_idx
        ON content_refresh_events(product_id, event_id)
    """,
)

_CREATE_PAGE_RETIREMENT_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS page_retirements (
        retirement_id INTEGER PRIMARY KEY AUTOINCREMENT,
        product_id TEXT NOT NULL
            REFERENCES products(product_id),
        source_attempt_id INTEGER NOT NULL
            REFERENCES attempts(attempt_id),
        source_hash TEXT NOT NULL
            CHECK (
                length(source_hash) = 64
                AND source_hash NOT GLOB '*[^0-9a-f]*'
            ),
        retired_content_schema_version INTEGER NOT NULL
            CHECK (retired_content_schema_version >= 0),
        wiki_path TEXT NOT NULL
            CHECK (length(wiki_path) BETWEEN 1 AND 1000),
        wiki_page_id INTEGER NOT NULL CHECK (wiki_page_id >= 1),
        page_content_sha256 TEXT NOT NULL
            CHECK (
                length(page_content_sha256) = 64
                AND page_content_sha256 NOT GLOB '*[^0-9a-f]*'
            ),
        page_updated_at TEXT,
        backup_sha256 TEXT NOT NULL
            CHECK (
                length(backup_sha256) = 64
                AND backup_sha256 NOT GLOB '*[^0-9a-f]*'
            ),
        backup_reference TEXT NOT NULL
            CHECK (length(backup_reference) BETWEEN 1 AND 1000),
        reason TEXT NOT NULL
            CHECK (length(reason) BETWEEN 1 AND 200),
        retired_at TEXT NOT NULL,
        resolved_at TEXT,
        resolution_reason TEXT
            CHECK (
                resolution_reason IS NULL
                OR length(resolution_reason) BETWEEN 1 AND 200
            ),
        CHECK (
            (resolved_at IS NULL AND resolution_reason IS NULL)
            OR
            (resolved_at IS NOT NULL AND resolution_reason IS NOT NULL)
        )
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS page_retirements_product_idx
        ON page_retirements(product_id, retirement_id)
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS page_retirements_active_product_idx
        ON page_retirements(product_id)
        WHERE resolved_at IS NULL
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS page_retirements_active_path_idx
        ON page_retirements(wiki_path)
        WHERE resolved_at IS NULL
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS page_retirements_active_page_id_idx
        ON page_retirements(wiki_page_id)
        WHERE resolved_at IS NULL
    """,
)

_CREATE_PARAMETER_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS verified_parameter_sets (
        parameter_set_id INTEGER PRIMARY KEY AUTOINCREMENT,
        product_id TEXT NOT NULL
            REFERENCES products(product_id) ON DELETE CASCADE,
        source_attempt_id INTEGER NOT NULL
            REFERENCES attempts(attempt_id) ON DELETE CASCADE,
        source_hash TEXT NOT NULL,
        pdf_url TEXT NOT NULL,
        pdf_sha256 TEXT NOT NULL
            CHECK (
                length(pdf_sha256) = 64
                AND pdf_sha256 NOT GLOB '*[^0-9a-f]*'
            ),
        parameters_json TEXT NOT NULL,
        parameter_count INTEGER NOT NULL CHECK (parameter_count >= 0),
        parameter_set_sha256 TEXT NOT NULL
            CHECK (
                length(parameter_set_sha256) = 64
                AND parameter_set_sha256 NOT GLOB '*[^0-9a-f]*'
            ),
        extractor_version TEXT NOT NULL
            CHECK (length(extractor_version) BETWEEN 1 AND 200),
        validation_policy_fingerprint TEXT NOT NULL
            CHECK (
                length(validation_policy_fingerprint) = 64
                AND validation_policy_fingerprint NOT GLOB '*[^0-9a-f]*'
            ),
        created_at TEXT NOT NULL,
        UNIQUE (
            product_id,
            source_attempt_id,
            source_hash,
            pdf_url,
            pdf_sha256,
            parameter_set_sha256,
            extractor_version,
            validation_policy_fingerprint
        )
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS verified_parameter_sets_product_idx
        ON verified_parameter_sets(product_id, source_attempt_id, parameter_set_id)
    """,
    """
    CREATE TABLE IF NOT EXISTS parameter_analysis_runs (
        analysis_run_id INTEGER PRIMARY KEY AUTOINCREMENT,
        parameter_set_id INTEGER NOT NULL
            REFERENCES verified_parameter_sets(parameter_set_id) ON DELETE CASCADE,
        request_fingerprint TEXT NOT NULL
            CHECK (
                length(request_fingerprint) = 64
                AND request_fingerprint NOT GLOB '*[^0-9a-f]*'
            ),
        attempt_number INTEGER NOT NULL CHECK (attempt_number >= 1),
        status TEXT NOT NULL
            CHECK (status IN ('started', 'completed', 'failed')),
        prompt_version TEXT NOT NULL
            CHECK (length(prompt_version) BETWEEN 1 AND 200),
        glossary_version TEXT NOT NULL
            CHECK (length(glossary_version) BETWEEN 1 AND 200),
        model TEXT NOT NULL CHECK (length(model) BETWEEN 1 AND 300),
        analysis_json TEXT,
        usage_json TEXT,
        error TEXT,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        CHECK (
            (
                status = 'started'
                AND analysis_json IS NULL
                AND usage_json IS NULL
                AND error IS NULL
                AND finished_at IS NULL
            )
            OR
            (
                status = 'completed'
                AND analysis_json IS NOT NULL
                AND usage_json IS NOT NULL
                AND error IS NULL
                AND finished_at IS NOT NULL
            )
            OR
            (
                status = 'failed'
                AND analysis_json IS NULL
                AND error IS NOT NULL
                AND finished_at IS NOT NULL
            )
        ),
        UNIQUE (parameter_set_id, request_fingerprint, attempt_number)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS parameter_analysis_runs_set_idx
        ON parameter_analysis_runs(parameter_set_id, analysis_run_id)
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS parameter_analysis_runs_started_idx
        ON parameter_analysis_runs(parameter_set_id, request_fingerprint)
        WHERE status = 'started'
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS parameter_analysis_runs_completed_idx
        ON parameter_analysis_runs(parameter_set_id, request_fingerprint)
        WHERE status = 'completed'
    """,
)

_CREATE_POSTGRES_PARAMETER_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS verified_parameter_sets (
        parameter_set_id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
        product_id TEXT NOT NULL
            REFERENCES products(product_id) ON DELETE CASCADE,
        source_attempt_id BIGINT NOT NULL
            REFERENCES attempts(attempt_id) ON DELETE CASCADE,
        source_hash TEXT NOT NULL,
        pdf_url TEXT NOT NULL,
        pdf_sha256 TEXT NOT NULL
            CHECK (pdf_sha256 ~ '^[0-9a-f]{64}$'),
        parameters_json TEXT NOT NULL,
        parameter_count INTEGER NOT NULL CHECK (parameter_count >= 0),
        parameter_set_sha256 TEXT NOT NULL
            CHECK (parameter_set_sha256 ~ '^[0-9a-f]{64}$'),
        extractor_version TEXT NOT NULL
            CHECK (length(extractor_version) BETWEEN 1 AND 200),
        validation_policy_fingerprint TEXT NOT NULL
            CHECK (validation_policy_fingerprint ~ '^[0-9a-f]{64}$'),
        created_at TIMESTAMPTZ NOT NULL,
        UNIQUE (
            product_id,
            source_attempt_id,
            source_hash,
            pdf_url,
            pdf_sha256,
            parameter_set_sha256,
            extractor_version,
            validation_policy_fingerprint
        )
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS verified_parameter_sets_product_idx
        ON verified_parameter_sets(product_id, source_attempt_id, parameter_set_id)
    """,
    """
    CREATE TABLE IF NOT EXISTS parameter_analysis_runs (
        analysis_run_id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
        parameter_set_id BIGINT NOT NULL
            REFERENCES verified_parameter_sets(parameter_set_id) ON DELETE CASCADE,
        request_fingerprint TEXT NOT NULL
            CHECK (request_fingerprint ~ '^[0-9a-f]{64}$'),
        attempt_number INTEGER NOT NULL CHECK (attempt_number >= 1),
        status TEXT NOT NULL
            CHECK (status IN ('started', 'completed', 'failed')),
        prompt_version TEXT NOT NULL
            CHECK (length(prompt_version) BETWEEN 1 AND 200),
        glossary_version TEXT NOT NULL
            CHECK (length(glossary_version) BETWEEN 1 AND 200),
        model TEXT NOT NULL CHECK (length(model) BETWEEN 1 AND 300),
        analysis_json TEXT,
        usage_json TEXT,
        error TEXT,
        started_at TIMESTAMPTZ NOT NULL,
        finished_at TIMESTAMPTZ,
        CHECK (
            (
                status = 'started'
                AND analysis_json IS NULL
                AND usage_json IS NULL
                AND error IS NULL
                AND finished_at IS NULL
            )
            OR
            (
                status = 'completed'
                AND analysis_json IS NOT NULL
                AND usage_json IS NOT NULL
                AND error IS NULL
                AND finished_at IS NOT NULL
            )
            OR
            (
                status = 'failed'
                AND analysis_json IS NULL
                AND error IS NOT NULL
                AND finished_at IS NOT NULL
            )
        ),
        UNIQUE (parameter_set_id, request_fingerprint, attempt_number)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS parameter_analysis_runs_set_idx
        ON parameter_analysis_runs(parameter_set_id, analysis_run_id)
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS parameter_analysis_runs_started_idx
        ON parameter_analysis_runs(parameter_set_id, request_fingerprint)
        WHERE status = 'started'
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS parameter_analysis_runs_completed_idx
        ON parameter_analysis_runs(parameter_set_id, request_fingerprint)
        WHERE status = 'completed'
    """,
)

_CREATE_POSTGRES_PAGE_RETIREMENT_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS page_retirements (
        retirement_id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
        basis TEXT NOT NULL DEFAULT 'synced_publication'
            CHECK (
                basis IN (
                    'synced_publication',
                    'failed_publication',
                    'legacy_managed_page'
                )
            ),
        product_id TEXT NOT NULL
            REFERENCES products(product_id),
        source_attempt_id BIGINT NOT NULL
            REFERENCES attempts(attempt_id),
        source_hash TEXT NOT NULL
            CHECK (source_hash ~ '^[0-9a-f]{64}$'),
        retired_content_schema_version INTEGER NOT NULL
            CHECK (retired_content_schema_version >= 0),
        wiki_path TEXT NOT NULL
            CHECK (length(wiki_path) BETWEEN 1 AND 1000),
        wiki_page_id BIGINT NOT NULL CHECK (wiki_page_id >= 1),
        page_content_sha256 TEXT NOT NULL
            CHECK (page_content_sha256 ~ '^[0-9a-f]{64}$'),
        page_updated_at TIMESTAMPTZ,
        backup_sha256 TEXT NOT NULL
            CHECK (backup_sha256 ~ '^[0-9a-f]{64}$'),
        backup_reference TEXT NOT NULL
            CHECK (length(backup_reference) BETWEEN 1 AND 1000),
        reason TEXT NOT NULL
            CHECK (length(reason) BETWEEN 1 AND 200),
        retired_at TIMESTAMPTZ NOT NULL,
        resolved_at TIMESTAMPTZ,
        resolution_reason TEXT
            CHECK (
                resolution_reason IS NULL
                OR length(resolution_reason) BETWEEN 1 AND 200
            ),
        CHECK (
            (resolved_at IS NULL AND resolution_reason IS NULL)
            OR
            (resolved_at IS NOT NULL AND resolution_reason IS NOT NULL)
        )
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS page_retirements_product_idx
        ON page_retirements(product_id, retirement_id)
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS page_retirements_active_product_idx
        ON page_retirements(product_id)
        WHERE resolved_at IS NULL
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS page_retirements_active_path_idx
        ON page_retirements(wiki_path)
        WHERE resolved_at IS NULL
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS page_retirements_active_page_id_idx
        ON page_retirements(wiki_page_id)
        WHERE resolved_at IS NULL
    """,
)

_CREATE_POSTGRES_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS products (
        product_id TEXT PRIMARY KEY,
        source_hash TEXT NOT NULL,
        source_updated_at TEXT,
        payload_json TEXT NOT NULL,
        status TEXT NOT NULL
            CHECK (status IN ('due', 'leased', 'backoff', 'synced')),
        next_run_at TEXT NOT NULL,
        consecutive_failures INTEGER NOT NULL DEFAULT 0
            CHECK (consecutive_failures >= 0),
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
        content_failure_cutoff_attempt_id BIGINT NOT NULL DEFAULT 0
            CHECK (content_failure_cutoff_attempt_id >= 0),
        leased_from_status TEXT
            CHECK (
                leased_from_status IS NULL
                OR leased_from_status IN ('due', 'backoff', 'synced')
            ),
        leased_from_next_run_at TEXT,
        content_schema_version INTEGER NOT NULL DEFAULT 0
            CHECK (content_schema_version >= 0),
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
        attempt_id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
        product_id TEXT NOT NULL
            REFERENCES products(product_id) ON DELETE CASCADE,
        source_hash TEXT NOT NULL,
        lease_token TEXT NOT NULL UNIQUE,
        worker_id TEXT NOT NULL,
        started_at TEXT NOT NULL,
        lease_until TEXT NOT NULL,
        finished_at TEXT,
        outcome TEXT,
        error TEXT,
        details_json TEXT,
        payload_json TEXT,
        search_started_at TEXT,
        search_urls_json TEXT,
        search_usage_json TEXT,
        extract_started_at TEXT,
        extract_urls_json TEXT,
        extract_usage_json TEXT,
        extract_success_urls_json TEXT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS attempts_product_idx
        ON attempts(product_id, attempt_id)
    """,
    """
    CREATE TABLE IF NOT EXISTS research_actions (
        action_id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
        attempt_id BIGINT NOT NULL
            REFERENCES attempts(attempt_id) ON DELETE CASCADE,
        round_number INTEGER NOT NULL CHECK (round_number >= 0),
        action TEXT NOT NULL CHECK (length(action) BETWEEN 1 AND 64),
        status TEXT NOT NULL
            CHECK (status IN ('started', 'completed', 'failed', 'uncertain')),
        request_fingerprint TEXT NOT NULL
            CHECK (
                length(request_fingerprint) = 64
                AND request_fingerprint ~ '^[0-9a-f]{64}$'
            ),
        started_at TEXT NOT NULL,
        finished_at TEXT,
        result_summary_json TEXT,
        credits DOUBLE PRECISION
            CHECK (credits IS NULL OR credits >= 0),
        error TEXT,
        scope_fingerprint TEXT,
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
    """
    CREATE INDEX IF NOT EXISTS research_actions_scope_idx
        ON research_actions(scope_fingerprint, status)
    """,
    """
    CREATE TABLE IF NOT EXISTS requeue_events (
        event_id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
        product_id TEXT NOT NULL
            REFERENCES products(product_id) ON DELETE CASCADE,
        cutoff_attempt_id BIGINT NOT NULL REFERENCES attempts(attempt_id),
        previous_status TEXT NOT NULL
            CHECK (previous_status IN ('backoff', 'synced')),
        previous_outcome TEXT NOT NULL,
        reason TEXT NOT NULL CHECK (length(reason) BETWEEN 1 AND 200),
        attempted_after TEXT,
        attempted_before TEXT,
        requeued_at TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS requeue_events_product_idx
        ON requeue_events(product_id, event_id)
    """,
    *_CREATE_POSTGRES_PARAMETER_SCHEMA,
    """
    CREATE TABLE IF NOT EXISTS content_refresh_events (
        event_id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
        product_id TEXT NOT NULL
            REFERENCES products(product_id) ON DELETE CASCADE,
        source_attempt_id BIGINT NOT NULL REFERENCES attempts(attempt_id),
        previous_content_schema_version INTEGER NOT NULL
            CHECK (previous_content_schema_version >= 0),
        content_schema_version INTEGER NOT NULL
            CHECK (content_schema_version >= 1),
        wiki_action TEXT NOT NULL
            CHECK (wiki_action IN ('unchanged', 'updated')),
        fact_diagnostics_json TEXT NOT NULL,
        parameter_set_id BIGINT
            REFERENCES verified_parameter_sets(parameter_set_id),
        analysis_run_id BIGINT
            REFERENCES parameter_analysis_runs(analysis_run_id),
        refreshed_at TIMESTAMPTZ NOT NULL,
        UNIQUE (product_id, source_attempt_id, content_schema_version)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS content_refresh_events_product_idx
        ON content_refresh_events(product_id, event_id)
    """,
    *_CREATE_POSTGRES_PAGE_RETIREMENT_SCHEMA,
    """
    CREATE TABLE IF NOT EXISTS state_metadata (
        singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
        schema_version INTEGER NOT NULL CHECK (schema_version >= 1),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE OR REPLACE FUNCTION json_valid(value TEXT)
    RETURNS BOOLEAN
    LANGUAGE plpgsql
    IMMUTABLE
    STRICT
    AS $function$
    BEGIN
        PERFORM value::jsonb;
        RETURN TRUE;
    EXCEPTION WHEN OTHERS THEN
        RETURN FALSE;
    END
    $function$
    """,
    """
    CREATE OR REPLACE FUNCTION json_extract(value TEXT, path TEXT)
    RETURNS TEXT
    LANGUAGE plpgsql
    IMMUTABLE
    STRICT
    AS $function$
    DECLARE
        key TEXT;
    BEGIN
        IF path !~ '^\\$\\.[A-Za-z_][A-Za-z0-9_]*$' THEN
            RETURN NULL;
        END IF;
        key := substring(path FROM 3);
        RETURN value::jsonb ->> key;
    EXCEPTION WHEN OTHERS THEN
        RETURN NULL;
    END
    $function$
    """,
)


def _postgres_placeholders(statement: str) -> str:
    """Translate DB-API qmark placeholders without touching SQL literals."""

    translated: list[str] = []
    index = 0
    quote: str | None = None
    dollar_tag: str | None = None
    while index < len(statement):
        if dollar_tag is not None:
            if statement.startswith(dollar_tag, index):
                translated.append(dollar_tag)
                index += len(dollar_tag)
                dollar_tag = None
            else:
                translated.append(statement[index])
                index += 1
            continue
        character = statement[index]
        if quote is not None:
            translated.append(character)
            index += 1
            if character == quote:
                if index < len(statement) and statement[index] == quote:
                    translated.append(statement[index])
                    index += 1
                else:
                    quote = None
            continue
        if character in {"'", '"'}:
            quote = character
            translated.append(character)
            index += 1
            continue
        if character == "$":
            match = re.match(r"\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$", statement[index:])
            if match is not None:
                dollar_tag = match.group(0)
                translated.append(dollar_tag)
                index += len(dollar_tag)
                continue
        if character == "?":
            translated.append("%s")
        else:
            translated.append(character)
        index += 1
    return "".join(translated)


class _PostgresConnection:
    """Small adapter preserving StateStore's qmark SQL call sites."""

    def __init__(self, connection: psycopg.Connection[Any]) -> None:
        self._connection = connection

    def execute(
        self,
        statement: str,
        parameters: Sequence[Any] | None = None,
    ) -> Any:
        return self._connection.execute(
            _postgres_placeholders(statement),
            () if parameters is None else parameters,
        )

    def commit(self) -> None:
        self._connection.commit()

    def rollback(self) -> None:
        self._connection.rollback()

    def close(self) -> None:
        self._connection.close()


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
    content_failure_cutoff_attempt_id: int
    leased_from_status: str | None
    leased_from_next_run_at: datetime | None
    content_schema_version: int


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
class RequeueEventRecord:
    """One successful operator requeue and its content-policy boundary."""

    event_id: int
    product_id: str
    cutoff_attempt_id: int
    previous_status: str
    previous_outcome: str
    reason: str
    attempted_after: datetime | None
    attempted_before: datetime | None
    requeued_at: datetime


@dataclass(frozen=True, slots=True)
class PageRetirementInput:
    """Operator-verified identity of one Wiki page that has been removed."""

    product_id: str
    source_attempt_id: int
    wiki_path: str
    wiki_page_id: int
    page_content_sha256: str
    page_updated_at: datetime | None = None
    basis: str = "synced_publication"


@dataclass(frozen=True, slots=True)
class PageRetirementRecord:
    """One append-only page-retirement audit event."""

    retirement_id: int
    basis: str
    product_id: str
    source_attempt_id: int
    source_hash: str
    retired_content_schema_version: int
    wiki_path: str
    wiki_page_id: int
    page_content_sha256: str
    page_updated_at: datetime | None
    backup_sha256: str
    backup_reference: str
    reason: str
    retired_at: datetime
    resolved_at: datetime | None
    resolution_reason: str | None


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
class ProviderRejectionRecord:
    """Latest provider-global rejection and its exact circuit expiry."""

    action: str
    research_status: str
    http_status: int
    error_type: str | None
    finished_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class ProviderErrorRecord:
    """Latest provider error, including errors without an HTTP response."""

    action: str
    research_status: str
    error_type: str
    http_status: int | None
    finished_at: datetime
    expires_at: datetime


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
    source_attempt_id: int
    content_schema_version: int


@dataclass(frozen=True, slots=True)
class ContentRefreshCandidate:
    """One page that can be re-rendered from an already verified decision."""

    product_id: str
    source_attempt_id: int
    source_hash: str
    payload: dict[str, Any]
    decision: dict[str, Any]
    wiki_path: str
    verified_at: datetime
    previous_content_schema_version: int
    fact_diagnostics: dict[str, Any]


@dataclass(frozen=True, slots=True)
class VerifiedParameterSetRecord:
    """One immutable, source-bound datasheet parameter extraction."""

    parameter_set_id: int
    product_id: str
    source_attempt_id: int
    source_hash: str
    pdf_url: str
    pdf_sha256: str
    parameters: list[Any]
    parameter_count: int
    parameter_set_sha256: str
    extractor_version: str
    validation_policy_fingerprint: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ParameterAnalysisRunRecord:
    """One append-only AI analysis attempt for a verified parameter set."""

    analysis_run_id: int
    parameter_set_id: int
    request_fingerprint: str
    attempt_number: int
    status: str
    prompt_version: str
    glossary_version: str
    model: str
    analysis: dict[str, Any] | None
    usage: dict[str, Any] | None
    error: str | None
    started_at: datetime
    finished_at: datetime | None


@dataclass(frozen=True, slots=True)
class ParameterAnalysisRunStartResult:
    """Return an analysis record and whether the caller may invoke the model."""

    record: ParameterAnalysisRunRecord
    should_execute: bool


@dataclass(frozen=True, slots=True)
class ContentRefreshEventRecord:
    """One successful existing-page-only managed-content refresh."""

    event_id: int
    product_id: str
    source_attempt_id: int
    previous_content_schema_version: int
    content_schema_version: int
    wiki_action: str
    fact_diagnostics: dict[str, Any]
    parameter_set_id: int | None
    analysis_run_id: int | None
    refreshed_at: datetime


def _utc(value: datetime | None = None) -> datetime:
    value = value or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _strict_utc_datetime(value: Any, *, name: str) -> datetime:
    """Validate an operator-supplied instant and normalize it to UTC."""

    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _outcome_filter(values: Any, *, name: str = "outcomes") -> tuple[str, ...]:
    if isinstance(values, (str, bytes, bytearray)) or not isinstance(
        values,
        Iterable,
    ):
        raise TypeError(f"{name} must be an iterable of outcome strings")
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            raise TypeError(f"{name} must contain only strings")
        outcome = value.strip().casefold()
        if not re.fullmatch(r"[a-z][a-z0-9_:-]{0,99}", outcome):
            raise ValueError(
                f"{name} must contain bounded lowercase outcome identifiers"
            )
        if outcome not in seen:
            seen.add(outcome)
            normalized.append(outcome)
    if not normalized:
        raise ValueError(f"{name} must not be empty")
    if len(normalized) > MAX_REQUEUE_OUTCOMES:
        raise ValueError(
            f"{name} may contain at most {MAX_REQUEUE_OUTCOMES} values"
        )
    return tuple(normalized)


def _content_schema_version(value: Any, *, allow_zero: bool) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("content_schema_version must be an integer")
    minimum = 0 if allow_zero else 1
    if not minimum <= value <= 1_000_000:
        raise ValueError(
            f"content_schema_version must be between {minimum} and 1000000"
        )
    return value


def _fact_diagnostics(value: Any) -> dict[str, Any]:
    """Validate count-only fact diagnostics without accepting source text."""

    if not isinstance(value, Mapping):
        raise TypeError("fact_diagnostics must be a mapping")
    allowed_fields = {
        "complete",
        "proposed",
        "retained",
        "rejected",
        "rejection_reasons",
    }
    unknown = set(value) - allowed_fields
    if unknown:
        raise ValueError(
            "fact_diagnostics contains unsupported fields: "
            + ", ".join(sorted(str(item) for item in unknown))
        )
    complete = value.get("complete")
    if not isinstance(complete, bool):
        raise TypeError("fact_diagnostics.complete must be a boolean")

    counts: dict[str, int] = {}
    for key in ("proposed", "retained", "rejected"):
        item = value.get(key)
        if isinstance(item, bool) or not isinstance(item, int):
            raise TypeError(f"fact_diagnostics.{key} must be an integer")
        if not 0 <= item <= MAX_FACT_DIAGNOSTIC_COUNT:
            raise ValueError(
                f"fact_diagnostics.{key} must be between 0 and "
                f"{MAX_FACT_DIAGNOSTIC_COUNT}"
            )
        counts[key] = item

    reasons_value = value.get("rejection_reasons")
    if not isinstance(reasons_value, Mapping):
        raise TypeError("fact_diagnostics.rejection_reasons must be a mapping")
    reasons: dict[str, int] = {}
    for raw_reason, raw_count in reasons_value.items():
        if not isinstance(raw_reason, str):
            raise TypeError("fact diagnostic reason codes must be strings")
        reason = raw_reason.strip().casefold()
        if reason not in FACT_DIAGNOSTIC_REASON_CODES:
            raise ValueError(f"unsupported fact diagnostic reason code: {reason}")
        if isinstance(raw_count, bool) or not isinstance(raw_count, int):
            raise TypeError("fact diagnostic reason counts must be integers")
        if not 0 <= raw_count <= MAX_FACT_DIAGNOSTIC_COUNT:
            raise ValueError(
                "fact diagnostic reason counts must be between 0 and "
                f"{MAX_FACT_DIAGNOSTIC_COUNT}"
            )
        if raw_count:
            reasons[reason] = raw_count

    if counts["proposed"] != counts["retained"] + counts["rejected"]:
        raise ValueError(
            "fact diagnostic proposed count must equal retained plus rejected"
        )
    if sum(reasons.values()) != counts["rejected"]:
        raise ValueError(
            "fact diagnostic reason counts must sum to rejected"
        )
    return {
        "complete": complete,
        **counts,
        "rejection_reasons": dict(sorted(reasons.items())),
    }


def _research_action_filter(
    values: Any,
    *,
    name: str = "actions",
) -> tuple[str, ...]:
    if isinstance(values, (str, bytes, bytearray)) or not isinstance(
        values,
        Iterable,
    ):
        raise TypeError(f"{name} must be an iterable of research actions")
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        action = _research_action_name(value)
        if action not in seen:
            seen.add(action)
            normalized.append(action)
    if not normalized:
        raise ValueError(f"{name} must not be empty")
    return tuple(normalized)


def _http_status_filter(
    values: Any,
    *,
    name: str = "http_statuses",
) -> tuple[int, ...]:
    if isinstance(values, (str, bytes, bytearray)) or not isinstance(
        values,
        Iterable,
    ):
        raise TypeError(f"{name} must be an iterable of integers")
    normalized: list[int] = []
    seen: set[int] = set()
    for value in values:
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 100 <= value <= 599
        ):
            raise ValueError(f"{name} must contain HTTP statuses from 100 to 599")
        if value not in seen:
            seen.add(value)
            normalized.append(value)
    if not normalized:
        raise ValueError(f"{name} must not be empty")
    return tuple(normalized)


def _error_type_filter(
    values: Any,
    *,
    name: str = "error_types",
) -> tuple[str, ...]:
    if isinstance(values, (str, bytes, bytearray)) or not isinstance(
        values,
        Iterable,
    ):
        raise TypeError(f"{name} must be an iterable of strings")
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            raise TypeError(f"{name} must contain only strings")
        error_type = value.strip()
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,99}", error_type) is None:
            raise ValueError(f"{name} must contain bounded class names")
        if error_type not in seen:
            seen.add(error_type)
            normalized.append(error_type)
    if not normalized:
        raise ValueError(f"{name} must not be empty")
    return tuple(normalized)


def next_month_start(value: datetime | None = None) -> datetime:
    """Return the first instant of the next UTC calendar month."""

    current = _utc(value)
    if current.month == 12:
        return datetime(current.year + 1, 1, 1, tzinfo=timezone.utc)
    return datetime(current.year, current.month + 1, 1, tzinfo=timezone.utc)


def _time_text(value: datetime) -> str:
    # A fixed-width UTC representation sorts chronologically as SQLite TEXT.
    return _utc(value).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse_time(value: str | datetime | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return _utc(value)
    if not isinstance(value, str):
        raise TypeError("stored timestamp must be a string, datetime, or None")
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


def _sha256_hex(value: Any, *, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    digest = value.strip().casefold()
    if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise ValueError(f"{name} must be a SHA-256 hex digest")
    return digest


def _page_retirement_reason(value: Any, *, name: str = "reason") -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    normalized = " ".join(value.split())
    if not normalized:
        raise ValueError(f"{name} must be non-empty")
    if len(normalized) > MAX_PAGE_RETIREMENT_REASON_LENGTH:
        raise ValueError(
            f"{name} must contain at most "
            f"{MAX_PAGE_RETIREMENT_REASON_LENGTH} characters"
        )
    return normalized


def _page_retirement_basis(value: Any) -> str:
    if not isinstance(value, str):
        raise TypeError("page retirement basis must be a string")
    basis = value.strip().casefold()
    if basis not in PAGE_RETIREMENT_BASES:
        raise ValueError(
            "page retirement basis must be one of: "
            + ", ".join(PAGE_RETIREMENT_BASES)
        )
    return basis


def _page_retirement_backup_reference(value: Any) -> str:
    if not isinstance(value, str):
        raise TypeError("backup_reference must be a string")
    reference = value.strip()
    if not reference:
        raise ValueError("backup_reference must be non-empty")
    if len(reference) > MAX_PAGE_RETIREMENT_BACKUP_REFERENCE_LENGTH:
        raise ValueError(
            "backup_reference must contain at most "
            f"{MAX_PAGE_RETIREMENT_BACKUP_REFERENCE_LENGTH} characters"
        )
    if any(
        ord(character) < 32 or ord(character) == 127
        for character in reference
    ):
        raise ValueError("backup_reference contains control characters")
    return reference


def _page_retirement_path(value: Any) -> str:
    if not isinstance(value, str):
        raise TypeError("wiki_path must be a string")
    path = value.strip().strip("/")
    if not path:
        raise ValueError("wiki_path must be non-empty")
    if len(path) > MAX_PAGE_RETIREMENT_PATH_LENGTH:
        raise ValueError(
            f"wiki_path must contain at most "
            f"{MAX_PAGE_RETIREMENT_PATH_LENGTH} characters"
        )
    if "\\" in path or "//" in path or any(
        ord(character) < 32 or ord(character) == 127 for character in path
    ):
        raise ValueError("wiki_path contains an unsafe path component")
    if any(segment in {"", ".", ".."} for segment in path.split("/")):
        raise ValueError("wiki_path contains an unsafe path component")
    return path


def _attempt_wiki_path(
    details_json: Any,
    *,
    require_path: bool,
) -> str | None:
    if details_json is None:
        if require_path:
            raise StateError(
                "page retirement source attempt lacks publication audit"
            )
        return None
    try:
        details = json.loads(details_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise StateError(
            "page retirement source attempt has invalid audit JSON"
        ) from exc
    if not isinstance(details, Mapping):
        if require_path:
            raise StateError(
                "page retirement source attempt lacks publication audit"
            )
        return None
    try:
        return _page_retirement_path(details.get("wiki_path"))
    except (TypeError, ValueError) as exc:
        if require_path:
            raise StateError(
                "page retirement source attempt lacks a stable Wiki path"
            ) from exc
        return None


def _page_retirement_input(value: Any) -> PageRetirementInput:
    if isinstance(value, PageRetirementInput):
        raw: Mapping[str, Any] = asdict(value)
    elif isinstance(value, Mapping):
        raw = value
    elif is_dataclass(value) and not isinstance(value, type):
        raw = asdict(value)
    else:
        raise TypeError(
            "page retirement entries must be mappings or dataclasses"
        )
    allowed = {
        "product_id",
        "source_attempt_id",
        "wiki_path",
        "wiki_page_id",
        "page_content_sha256",
        "page_updated_at",
        "basis",
    }
    unknown = set(raw) - allowed
    if unknown:
        raise ValueError(
            "page retirement entry contains unsupported fields: "
            + ", ".join(sorted(str(field) for field in unknown))
        )
    product_value = raw.get("product_id")
    if not isinstance(product_value, str):
        raise TypeError("product_id must be a string")
    product_id = product_value.strip()
    if not product_id or len(product_id) > 500:
        raise ValueError("product_id must be a bounded non-empty value")
    page_updated_value = raw.get("page_updated_at")
    page_updated_at = (
        None
        if page_updated_value is None
        else _strict_utc_datetime(
            page_updated_value,
            name="page_updated_at",
        )
    )
    return PageRetirementInput(
        product_id=product_id,
        source_attempt_id=_positive_record_id(
            raw.get("source_attempt_id"),
            name="source_attempt_id",
        ),
        wiki_path=_page_retirement_path(raw.get("wiki_path")),
        wiki_page_id=_positive_record_id(
            raw.get("wiki_page_id"),
            name="wiki_page_id",
        ),
        page_content_sha256=_sha256_hex(
            raw.get("page_content_sha256"),
            name="page_content_sha256",
        ),
        page_updated_at=page_updated_at,
        basis=_page_retirement_basis(
            raw.get("basis", "synced_publication")
        ),
    )


def _bounded_parameter_text(
    value: Any,
    *,
    name: str,
    max_length: int,
) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    normalized = " ".join(value.split())
    if not normalized:
        raise ValueError(f"{name} must be non-empty")
    if len(normalized) > max_length:
        raise ValueError(f"{name} exceeds {max_length} characters")
    return normalized


def _verified_parameters(
    value: Any,
) -> tuple[list[Any], str, str]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(
        value,
        Sequence,
    ):
        raise TypeError("parameters must be a sequence")
    if len(value) > MAX_VERIFIED_PARAMETER_COUNT:
        raise ValueError(
            "parameters may contain at most "
            f"{MAX_VERIFIED_PARAMETER_COUNT} entries"
        )
    serialized = _canonical_json(list(value))
    if len(serialized.encode("utf-8")) > MAX_VERIFIED_PARAMETERS_JSON_BYTES:
        raise ValueError(
            "canonical parameters exceed "
            f"{MAX_VERIFIED_PARAMETERS_JSON_BYTES} bytes"
        )
    normalized = json.loads(serialized)
    return (
        normalized,
        serialized,
        hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
    )


def _parameter_analysis_mapping(
    value: Any,
    *,
    name: str,
    max_bytes: int,
) -> tuple[dict[str, Any], str]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be a mapping")
    serialized = _canonical_json(value)
    if len(serialized.encode("utf-8")) > max_bytes:
        raise ValueError(f"canonical {name} exceeds {max_bytes} bytes")
    normalized = json.loads(serialized)
    return normalized, serialized


def _parameter_analysis_error(value: Any) -> str:
    if not isinstance(value, str):
        raise TypeError("parameter analysis error must be a string")
    error = " ".join(value.split())
    if not error:
        raise ValueError("failed parameter analysis runs need an error")
    if len(error) > MAX_PARAMETER_ANALYSIS_ERROR_LENGTH:
        raise ValueError(
            "parameter analysis error exceeds "
            f"{MAX_PARAMETER_ANALYSIS_ERROR_LENGTH} characters"
        )
    return error


def _positive_record_id(value: Any, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if value < 1:
        raise ValueError(f"{name} must be positive")
    return value


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
    # Token metadata uses names such as ``prompt_tokens`` that deliberately
    # overlap the generic response-body key detector. Remove this one
    # explicitly bounded field while scanning all other summary values, then
    # restore it for the numeric whitelist validation below.
    usage_totals_marker = object()
    raw_usage_totals = summary.pop(
        "usage_totals",
        usage_totals_marker,
    )
    _reject_research_bodies(summary)
    if raw_usage_totals is not usage_totals_marker:
        summary["usage_totals"] = raw_usage_totals
    allowed_fields = {
        "search": frozenset(
            {
                "queries",
                "candidate_urls",
                "request_ids",
                "provider_requests",
                "completed_provider_requests",
                "known_partial_credits",
                "provider_fingerprint",
                "http_status",
                "error_type",
            }
        ),
        "extract": frozenset(
            {
                "submitted_urls",
                "successful_urls",
                "provider_requests",
                "completed_provider_requests",
                "known_partial_credits",
                "provider_fingerprint",
                "http_status",
                "error_type",
                "direct_pdf",
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
                "error_category",
                "finish_reasons",
                "usage_totals",
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
    if "direct_pdf" in summary:
        direct_pdf = summary["direct_pdf"]
        if not isinstance(direct_pdf, Mapping):
            raise TypeError("direct_pdf must be a mapping")
        direct_pdf_fields = frozenset(
            {
                "attempted",
                "used",
                "failed",
                "identity_mismatch",
            }
        )
        unsupported_direct_pdf = set(direct_pdf) - direct_pdf_fields
        if unsupported_direct_pdf:
            raise ValueError(
                "direct_pdf contains unsupported fields: "
                + ", ".join(
                    sorted(
                        str(field)
                        for field in unsupported_direct_pdf
                    )
                )
            )
        if set(direct_pdf) != direct_pdf_fields:
            missing_direct_pdf = direct_pdf_fields - set(direct_pdf)
            raise ValueError(
                "direct_pdf is missing fields: "
                + ", ".join(sorted(missing_direct_pdf))
            )
        normalized_direct_pdf = {
            field: _bounded_summary_count(
                direct_pdf[field],
                name=f"direct_pdf.{field}",
            )
            for field in sorted(direct_pdf_fields)
        }
        if normalized_direct_pdf["used"] > normalized_direct_pdf["attempted"]:
            raise ValueError("direct_pdf.used cannot exceed attempted")
        if normalized_direct_pdf["failed"] > normalized_direct_pdf["attempted"]:
            raise ValueError("direct_pdf.failed cannot exceed attempted")
        if (
            normalized_direct_pdf["identity_mismatch"]
            > normalized_direct_pdf["attempted"]
        ):
            raise ValueError(
                "direct_pdf.identity_mismatch cannot exceed attempted"
            )
        if (
            normalized_direct_pdf["used"]
            + normalized_direct_pdf["failed"]
            + normalized_direct_pdf["identity_mismatch"]
            != normalized_direct_pdf["attempted"]
        ):
            raise ValueError(
                "direct_pdf outcomes must sum to attempted"
            )
        summary["direct_pdf"] = normalized_direct_pdf
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
    if "error_category" in summary:
        summary["error_category"] = _bounded_summary_text(
            summary["error_category"],
            name="error_category",
            maximum=50,
            allowed=_RESEARCH_AI_ERROR_CATEGORIES,
        )
    if "finish_reasons" in summary:
        finish_reasons = summary["finish_reasons"]
        if (
            isinstance(finish_reasons, (str, bytes, bytearray))
            or not isinstance(finish_reasons, Sequence)
        ):
            raise TypeError("finish_reasons must be an array of strings")
        if len(finish_reasons) > 2:
            raise ValueError("finish_reasons may contain at most 2 items")
        normalized_reasons: list[str] = []
        for reason in finish_reasons:
            if not isinstance(reason, str):
                raise TypeError("finish_reasons must be an array of strings")
            normalized_reason = reason.strip().casefold()
            if re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", normalized_reason) is None:
                raise ValueError(
                    "finish_reasons must contain bounded identifiers"
                )
            normalized_reasons.append(normalized_reason)
        summary["finish_reasons"] = normalized_reasons
    if "usage_totals" in summary:
        usage_totals = summary["usage_totals"]
        if not isinstance(usage_totals, Mapping):
            raise TypeError("usage_totals must be a mapping")
        unsupported_usage = set(usage_totals) - _RESEARCH_AI_USAGE_KEYS
        if unsupported_usage:
            raise ValueError(
                "usage_totals contains unsupported token fields: "
                + ", ".join(
                    sorted(str(field) for field in unsupported_usage)
                )
            )
        normalized_usage: dict[str, int] = {}
        for field, value in usage_totals.items():
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 0 <= value <= MAX_RESEARCH_AI_TOKEN_USAGE
            ):
                raise ValueError(
                    "usage_totals values must be integer token counts between "
                    f"0 and {MAX_RESEARCH_AI_TOKEN_USAGE}"
                )
            normalized_usage[str(field)] = value
        summary["usage_totals"] = normalized_usage
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
    if outcome == "content_quarantined":
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
    """PostgreSQL/SQLite product queue, lease manager, and audit log."""

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
        self._timeout = timeout
        self._keeper: sqlite3.Connection | None = None
        parsed = urllib.parse.urlsplit(supplied_path)
        if parsed.scheme in {"postgres", "postgresql"}:
            if not parsed.hostname or not parsed.path.strip("/"):
                raise ValueError(
                    "PostgreSQL state URL must include a host and database"
                )
            self.backend = "postgresql"
            host = parsed.hostname
            if ":" in host:
                host = f"[{host}]"
            database_name = urllib.parse.unquote(parsed.path.strip("/"))
            self.location = (
                f"postgresql://{host}:{parsed.port or 5432}/{database_name}"
            )
            self.path = self.location
            self._database = supplied_path
            self._uri = False
        elif supplied_path == ":memory:":
            # Methods intentionally use short-lived connections.  A shared-cache
            # URI plus a keeper preserves those semantics for in-memory tests.
            self.backend = "sqlite"
            self.location = ":memory:"
            self.path = supplied_path
            self._database = f"file:pv_wiki_state_{uuid.uuid4().hex}?mode=memory&cache=shared"
            self._uri = True
            self._keeper = self._new_connection()
        else:
            self.backend = "sqlite"
            database_path = Path(supplied_path).expanduser()
            self._create_parent_directories(database_path.parent)
            self._create_database_file(database_path)
            self.path = str(database_path)
            self.location = self.path
            self._database = str(database_path)
            self._uri = False
        self.migrate()

    def _new_connection(self) -> Any:
        if self.backend == "postgresql":
            connect_timeout = max(1, math.ceil(self._timeout))
            raw_connection = psycopg.connect(
                self._database,
                autocommit=True,
                connect_timeout=connect_timeout,
                row_factory=dict_row,
            )
            raw_connection.execute(
                "SELECT set_config('lock_timeout', %s, false)",
                (f"{int(self._timeout * 1000)}ms",),
            )
            raw_connection.execute(
                "SELECT set_config('statement_timeout', %s, false)",
                ("30000ms",),
            )
            return _PostgresConnection(raw_connection)
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
            if self.backend == "postgresql":
                connection.execute("BEGIN")
                connection.execute(
                    "SELECT pg_advisory_xact_lock(?)",
                    (_POSTGRES_ADVISORY_LOCK,),
                )
            else:
                connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()

    @contextmanager
    def publication_fence(self) -> Any:
        """Serialize externally visible Wiki publication across processes.

        This lock is deliberately independent from the transaction-scoped
        scheduler lock. Callers may therefore hold it while performing Wiki.js
        I/O and then safely enter :meth:`record_outcome` or
        :meth:`record_content_refresh` without self-deadlocking.
        """

        if self.backend == "postgresql":
            with self._connection() as connection:
                # Publication can legitimately include a bounded remote Wiki
                # request. Do not inherit the short database lock/statement
                # timeouts while waiting for another publisher to finish.
                connection.execute(
                    "SELECT set_config('lock_timeout', ?, false)",
                    ("0",),
                )
                connection.execute(
                    "SELECT set_config('statement_timeout', ?, false)",
                    ("0",),
                )
                connection.execute(
                    "SELECT pg_advisory_lock(?)",
                    (_POSTGRES_PUBLICATION_LOCK,),
                )
                try:
                    yield
                finally:
                    connection.execute(
                        "SELECT pg_advisory_unlock(?)",
                        (_POSTGRES_PUBLICATION_LOCK,),
                    )
            return

        if self.location == ":memory:":
            with _MEMORY_PUBLICATION_LOCK:
                yield
            return

        lock_path = f"{self.path}.publication.lock"
        flags = os.O_CREAT | os.O_RDWR
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(lock_path, flags, 0o600)
        except OSError as exc:
            raise StateError(
                f"cannot open SQLite publication lock: {lock_path}"
            ) from exc
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

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

        if self.backend == "postgresql":
            return self._migrate_postgresql()
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
                # Only URLs the search provider extracted may become evidence.
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
            if current < 8:
                # An operator requeue starts a new content-policy epoch without
                # deleting immutable attempts. The event ledger preserves why
                # the boundary was moved and which completed attempt it follows.
                connection.execute(
                    """
                    ALTER TABLE products
                    ADD COLUMN content_failure_cutoff_attempt_id
                        INTEGER NOT NULL DEFAULT 0
                        CHECK (content_failure_cutoff_attempt_id >= 0)
                    """
                )
                for statement in _CREATE_REQUEUE_EVENT_SCHEMA:
                    connection.execute(statement)
                # Older workers recorded every AI exception as ``uncertain``.
                # AIInvalidOutputError is narrower: parsing/contract validation
                # happens only after the provider response arrived, so delivery
                # is known and replay suppression must not treat it like an
                # ambiguous network outcome. Preserve the original audit body,
                # error, and timestamps while making unknown legacy credits
                # explicitly zero, matching the current failure contract.
                connection.execute(
                    """
                    UPDATE research_actions
                    SET status = 'failed', credits = COALESCE(credits, 0)
                    WHERE action = 'ai'
                      AND status = 'uncertain'
                      AND CASE
                          WHEN json_valid(result_summary_json)
                          THEN CAST(json_extract(
                              result_summary_json,
                              '$.error_type'
                          ) AS TEXT)
                          ELSE NULL
                      END = 'AIInvalidOutputError'
                    """
                )
                connection.execute("PRAGMA user_version = 8")
                current = 8
            if current < 9:
                # A system-wide provider pause may be discovered only after a
                # product is leased. Persist the exact runnable queue class and
                # due time so releasing that lease is product-neutral. Existing
                # active v8 leases have no class snapshot; treating them as due
                # is the conservative recovery that cannot hide runnable work.
                connection.execute(
                    """
                    ALTER TABLE products
                    ADD COLUMN leased_from_status TEXT
                        CHECK (
                            leased_from_status IS NULL
                            OR leased_from_status IN ('due', 'backoff', 'synced')
                        )
                    """
                )
                connection.execute(
                    """
                    ALTER TABLE products
                    ADD COLUMN leased_from_next_run_at TEXT
                    """
                )
                connection.execute(
                    """
                    UPDATE products
                    SET leased_from_status = 'due',
                        leased_from_next_run_at = next_run_at
                    WHERE status = 'leased'
                    """
                )
                connection.execute("PRAGMA user_version = 9")
                current = 9
            if current < 10:
                # Content-only migrations must not wake products or replay paid
                # research. Version zero denotes a page written before managed
                # content versioning was introduced.
                connection.execute(
                    """
                    ALTER TABLE products
                    ADD COLUMN content_schema_version
                        INTEGER NOT NULL DEFAULT 0
                        CHECK (content_schema_version >= 0)
                    """
                )
                for statement in _CREATE_CONTENT_REFRESH_EVENT_SCHEMA:
                    connection.execute(statement)
                connection.execute("PRAGMA user_version = 10")
                current = 10
            if current < 11:
                # Datasheet-derived parameters and their AI interpretation are
                # append-only evidence. Refresh events hold nullable references
                # so pre-v11 audit rows remain valid.
                for statement in _CREATE_PARAMETER_SCHEMA:
                    connection.execute(statement)
                connection.execute(
                    """
                    ALTER TABLE content_refresh_events
                    ADD COLUMN parameter_set_id INTEGER
                        REFERENCES verified_parameter_sets(parameter_set_id)
                    """
                )
                connection.execute(
                    """
                    ALTER TABLE content_refresh_events
                    ADD COLUMN analysis_run_id INTEGER
                        REFERENCES parameter_analysis_runs(analysis_run_id)
                    """
                )
                connection.execute("PRAGMA user_version = 11")
                current = 11
            if current < 12:
                # Removed managed pages are tombstoned independently of the
                # queue status. This preserves the immutable publication
                # attempt while ensuring catalogue refreshes cannot silently
                # recreate an operator-retired page.
                for statement in _CREATE_PAGE_RETIREMENT_SCHEMA:
                    connection.execute(statement)
                connection.execute("PRAGMA user_version = 12")
                current = 12
            if current < 13:
                # Distinguish proven successful publications from pages left
                # by failed publication attempts and older managed pages. All
                # v12 rows were accepted only under the synced-publication
                # contract, so that is their lossless migration default.
                connection.execute(
                    """
                    ALTER TABLE page_retirements
                    ADD COLUMN basis TEXT NOT NULL
                        DEFAULT 'synced_publication'
                        CHECK (
                            basis IN (
                                'synced_publication',
                                'failed_publication',
                                'legacy_managed_page'
                            )
                        )
                    """
                )
                connection.execute("PRAGMA user_version = 13")
                current = 13
        return current

    def _migrate_postgresql(self) -> int:
        """Create/validate the current schema in a dedicated PostgreSQL DB."""

        current: int | None = None
        with self._connection() as connection:
            metadata = connection.execute(
                "SELECT to_regclass('state_metadata') AS metadata_table"
            ).fetchone()
            if metadata is not None and metadata["metadata_table"] is not None:
                row = connection.execute(
                    """
                    SELECT schema_version
                    FROM state_metadata
                    WHERE singleton = TRUE
                    """
                ).fetchone()
                if row is None:
                    raise StateError(
                        "PostgreSQL state schema metadata is missing"
                    )
                current = int(row["schema_version"])
                if current == SCHEMA_VERSION:
                    return current
                if current > SCHEMA_VERSION or current < 9:
                    return self._validate_postgresql_schema_version(current)

        if current in {9, 10, 11, 12}:
            with self._write_transaction() as connection:
                # Another initializer may have completed this migration after
                # the unlocked discovery read above. Recheck while holding the
                # global transaction advisory lock before issuing any DDL.
                row = connection.execute(
                    """
                    SELECT schema_version
                    FROM state_metadata
                    WHERE singleton = TRUE
                    """
                ).fetchone()
                if row is None:
                    raise StateError(
                        "PostgreSQL state schema metadata is missing"
                    )
                locked_current = int(row["schema_version"])
                if locked_current == SCHEMA_VERSION:
                    return locked_current
                if locked_current not in {9, 10, 11, 12}:
                    return self._validate_postgresql_schema_version(
                        locked_current
                    )
                if locked_current == 9:
                    connection.execute(
                        """
                        ALTER TABLE products
                        ADD COLUMN content_schema_version
                            INTEGER NOT NULL DEFAULT 0
                            CHECK (content_schema_version >= 0)
                        """
                    )

                if locked_current in {9, 10}:
                    for statement in _CREATE_POSTGRES_PARAMETER_SCHEMA:
                        connection.execute(statement)

                if locked_current == 9:
                    connection.execute(
                        """
                        CREATE TABLE content_refresh_events (
                            event_id BIGINT GENERATED BY DEFAULT AS IDENTITY
                                PRIMARY KEY,
                            product_id TEXT NOT NULL
                                REFERENCES products(product_id) ON DELETE CASCADE,
                            source_attempt_id BIGINT NOT NULL
                                REFERENCES attempts(attempt_id),
                            previous_content_schema_version INTEGER NOT NULL
                                CHECK (previous_content_schema_version >= 0),
                            content_schema_version INTEGER NOT NULL
                                CHECK (content_schema_version >= 1),
                            wiki_action TEXT NOT NULL
                                CHECK (wiki_action IN ('unchanged', 'updated')),
                            fact_diagnostics_json TEXT NOT NULL,
                            parameter_set_id BIGINT
                                REFERENCES verified_parameter_sets(
                                    parameter_set_id
                                ),
                            analysis_run_id BIGINT
                                REFERENCES parameter_analysis_runs(
                                    analysis_run_id
                                ),
                            refreshed_at TIMESTAMPTZ NOT NULL,
                            UNIQUE (
                                product_id,
                                source_attempt_id,
                                content_schema_version
                            )
                        )
                        """
                    )
                    connection.execute(
                        """
                        CREATE INDEX content_refresh_events_product_idx
                        ON content_refresh_events(product_id, event_id)
                        """
                    )
                elif locked_current == 10:
                    connection.execute(
                        """
                        ALTER TABLE content_refresh_events
                        ADD COLUMN IF NOT EXISTS parameter_set_id BIGINT
                            REFERENCES verified_parameter_sets(parameter_set_id)
                        """
                    )
                    connection.execute(
                        """
                        ALTER TABLE content_refresh_events
                        ADD COLUMN IF NOT EXISTS analysis_run_id BIGINT
                            REFERENCES parameter_analysis_runs(analysis_run_id)
                        """
                    )
                if locked_current <= 11:
                    for statement in _CREATE_POSTGRES_PAGE_RETIREMENT_SCHEMA:
                        connection.execute(statement)
                else:
                    connection.execute(
                        """
                        ALTER TABLE page_retirements
                        ADD COLUMN basis TEXT NOT NULL
                            DEFAULT 'synced_publication'
                            CHECK (
                                basis IN (
                                    'synced_publication',
                                    'failed_publication',
                                    'legacy_managed_page'
                                )
                            )
                        """
                    )
                connection.execute(
                    """
                    UPDATE state_metadata
                    SET schema_version = ?, updated_at = now()
                    WHERE singleton = TRUE AND schema_version = ?
                    """,
                    (SCHEMA_VERSION, locked_current),
                )
            return SCHEMA_VERSION

        with self._write_transaction() as connection:
            for statement in _CREATE_POSTGRES_SCHEMA:
                connection.execute(statement)
            connection.execute(
                """
                INSERT INTO state_metadata (singleton, schema_version)
                VALUES (TRUE, ?)
                ON CONFLICT (singleton) DO NOTHING
                """,
                (SCHEMA_VERSION,),
            )
            row = connection.execute(
                """
                SELECT schema_version
                FROM state_metadata
                WHERE singleton = TRUE
                """
            ).fetchone()
            if row is None:
                raise StateError("PostgreSQL state schema metadata is missing")
            return self._validate_postgresql_schema_version(
                int(row["schema_version"])
            )

    @staticmethod
    def _validate_postgresql_schema_version(current: int) -> int:
        if current != SCHEMA_VERSION:
            direction = (
                "newer than"
                if current > SCHEMA_VERSION
                else "older than"
            )
            raise SchemaVersionError(
                f"state schema {current} is {direction} supported "
                f"version {SCHEMA_VERSION}"
            )
        return current

    @property
    def schema_version(self) -> int:
        with self._connection() as connection:
            if self.backend == "postgresql":
                row = connection.execute(
                    """
                    SELECT schema_version
                    FROM state_metadata
                    WHERE singleton = TRUE
                    """
                ).fetchone()
                if row is None:
                    raise StateError("PostgreSQL state schema metadata is missing")
                return int(row["schema_version"])
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
        """Upsert a catalogue batch in one state transaction."""

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
            """
            SELECT
                p.source_hash,
                p.status,
                EXISTS (
                    SELECT 1
                    FROM page_retirements AS retirement
                    WHERE retirement.product_id = p.product_id
                      AND retirement.resolved_at IS NULL
                ) AS page_retired
            FROM products AS p
            WHERE p.product_id = ?
            """,
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

        if bool(existing["page_retired"]):
            # Keep the current catalogue snapshot for operator inspection, but
            # do not wake research or publication while the page tombstone is
            # active. Resolving the retirement explicitly makes it due.
            connection.execute(
                """
                UPDATE products
                SET source_hash = ?, source_updated_at = ?, payload_json = ?,
                    consecutive_failures = 0, last_error = NULL, updated_at = ?
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
            return UpsertResult(
                product_id,
                source_hash,
                False,
                True,
                False,
                str(existing["status"]),
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
                leased_source_hash = NULL, leased_from_status = NULL,
                leased_from_next_run_at = NULL, reschedule_requested = 0,
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

    def resume_search_quota_waits(self, *, now: datetime | None = None) -> int:
        """Make Exa-quota-paused products due after a key or budget change."""

        now_text = _time_text(_utc(now))
        with self._write_transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE products
                SET status = 'due', next_run_at = ?, updated_at = ?
                WHERE status = 'backoff'
                  AND last_outcome IN (
                      'tavily_quota_exhausted', -- Legacy persisted value.
                      'search_quota_exhausted'
                  )
                  AND lease_token IS NULL
                  AND NOT EXISTS (
                      SELECT 1
                      FROM page_retirements AS retirement
                      WHERE retirement.product_id = products.product_id
                        AND retirement.resolved_at IS NULL
                  )
                """,
                (now_text, now_text),
            )
        return max(cursor.rowcount, 0)

    def requeue_products(
        self,
        outcomes: Iterable[str],
        *,
        reason: str,
        attempted_after: datetime | None = None,
        attempted_before: datetime | None = None,
        limit: int = 1000,
        now: datetime | None = None,
    ) -> int:
        """Selectively wake old finished outcomes without rewriting their audit.

        The optional attempt window applies to the latest finished attempt for
        each product. ``attempted_after`` is inclusive and
        ``attempted_before`` is exclusive. Only ``backoff`` or ``synced``
        products without a lease are changed, which makes repeated calls
        idempotent and prevents an operator action from stealing active work.

        ``reason`` is a bounded operator policy marker stored in an append-only
        requeue event. Each successful requeue also advances the product's
        content-failure cutoff to its latest completed attempt. Old attempts
        remain immutable and queryable, but no longer count toward the new
        content-policy epoch.
        """

        normalized_outcomes = _outcome_filter(outcomes)
        if not isinstance(reason, str):
            raise TypeError("reason must be a string")
        normalized_reason = " ".join(reason.split())
        if not normalized_reason:
            raise ValueError("reason must be non-empty")
        if len(normalized_reason) > MAX_REQUEUE_REASON_LENGTH:
            raise ValueError(
                f"reason must contain at most {MAX_REQUEUE_REASON_LENGTH} characters"
            )
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise TypeError("limit must be an integer")
        if not 1 <= limit <= 10_000:
            raise ValueError("limit must be between 1 and 10000")
        lower = (
            None
            if attempted_after is None
            else _strict_utc_datetime(
                attempted_after,
                name="attempted_after",
            )
        )
        upper = (
            None
            if attempted_before is None
            else _strict_utc_datetime(
                attempted_before,
                name="attempted_before",
            )
        )
        if lower is not None and upper is not None and lower >= upper:
            raise ValueError("attempted_after must be earlier than attempted_before")

        timestamp = _utc(now)
        now_text = _time_text(timestamp)
        placeholders = ", ".join("?" for _ in normalized_outcomes)
        clauses = [
            "p.status IN ('backoff', 'synced')",
            "p.lease_token IS NULL",
            "NOT EXISTS ("
            "SELECT 1 FROM page_retirements AS retirement "
            "WHERE retirement.product_id = p.product_id "
            "AND retirement.resolved_at IS NULL)",
            f"p.last_outcome IN ({placeholders})",
            "latest.outcome = p.last_outcome",
        ]
        parameters: list[Any] = list(normalized_outcomes)
        if lower is not None:
            clauses.append("latest.started_at >= ?")
            parameters.append(_time_text(lower))
        if upper is not None:
            clauses.append("latest.started_at < ?")
            parameters.append(_time_text(upper))
        parameters.append(limit)

        with self._write_transaction() as connection:
            rows = connection.execute(
                f"""
                SELECT
                    p.product_id,
                    p.status AS previous_status,
                    p.last_outcome AS previous_outcome,
                    latest.attempt_id AS cutoff_attempt_id
                FROM products AS p
                JOIN attempts AS latest
                  ON latest.attempt_id = (
                      SELECT candidate.attempt_id
                      FROM attempts AS candidate
                      WHERE candidate.product_id = p.product_id
                        AND candidate.finished_at IS NOT NULL
                        AND substr(candidate.outcome, 1, 7) <> 'system_'
                      ORDER BY candidate.attempt_id DESC
                      LIMIT 1
                  )
                WHERE {' AND '.join(clauses)}
                ORDER BY latest.started_at, latest.attempt_id, p.product_id
                LIMIT ?
                """,
                parameters,
            ).fetchall()
            if not rows:
                return 0
            updated_count = 0
            attempted_after_text = (
                None if lower is None else _time_text(lower)
            )
            attempted_before_text = (
                None if upper is None else _time_text(upper)
            )
            for row in rows:
                updated = connection.execute(
                    """
                    UPDATE products
                    SET status = 'due', next_run_at = ?, updated_at = ?,
                        content_failure_cutoff_attempt_id = ?
                    WHERE product_id = ?
                      AND status IN ('backoff', 'synced')
                      AND lease_token IS NULL
                      AND NOT EXISTS (
                          SELECT 1
                          FROM page_retirements AS retirement
                          WHERE retirement.product_id = products.product_id
                            AND retirement.resolved_at IS NULL
                      )
                    """,
                    (
                        now_text,
                        now_text,
                        int(row["cutoff_attempt_id"]),
                        str(row["product_id"]),
                    ),
                )
                if updated.rowcount != 1:
                    continue
                connection.execute(
                    """
                    INSERT INTO requeue_events (
                        product_id, cutoff_attempt_id, previous_status,
                        previous_outcome, reason, attempted_after,
                        attempted_before, requeued_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(row["product_id"]),
                        int(row["cutoff_attempt_id"]),
                        str(row["previous_status"]),
                        str(row["previous_outcome"]),
                        normalized_reason,
                        attempted_after_text,
                        attempted_before_text,
                        now_text,
                    ),
                )
                updated_count += 1
        return updated_count

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
                  AND NOT EXISTS (
                      SELECT 1
                      FROM page_retirements AS retirement
                      WHERE retirement.product_id = products.product_id
                        AND retirement.resolved_at IS NULL
                  )
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
        This inexpensive check should be made before spending an Exa request.
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

        if self.active_page_retirement(product_id) is not None:
            return PrecheckResult(
                product_id,
                False,
                "page_retired",
                row.status,
                row.source_hash,
                row.next_run_at,
            )

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
            sequence_row = connection.execute(
                """
                SELECT COALESCE(MAX(attempt_id), 0) AS seq
                FROM attempts
                """
            ).fetchone()
            lease_sequence = int(sequence_row["seq"])
            cycle_size = DUE_QUEUE_WEIGHT + BACKOFF_QUEUE_WEIGHT
            preferred_status = (
                "backoff"
                if lease_sequence % cycle_size >= DUE_QUEUE_WEIGHT
                else "due"
            )
            row = connection.execute(
                """
                SELECT * FROM products
                WHERE status IN ('due', 'backoff', 'synced')
                  AND next_run_at <= ?
                  AND NOT EXISTS (
                      SELECT 1
                      FROM page_retirements AS retirement
                      WHERE retirement.product_id = products.product_id
                        AND retirement.resolved_at IS NULL
                  )
                ORDER BY
                    CASE
                        WHEN status = ? THEN 0
                        WHEN status IN ('due', 'backoff') THEN 1
                        ELSE 2
                    END,
                    next_run_at,
                    updated_at,
                    product_id
                LIMIT 1
                """,
                (now_text, preferred_status),
            ).fetchone()
            if row is None:
                return None

            token = uuid.uuid4().hex
            updated = connection.execute(
                """
                UPDATE products
                SET status = 'leased', lease_token = ?, lease_owner = ?,
                    lease_until = ?, leased_source_hash = source_hash,
                    leased_from_status = status,
                    leased_from_next_run_at = next_run_at,
                    reschedule_requested = 0, last_attempt_at = ?, updated_at = ?
                WHERE product_id = ?
                  AND status IN ('due', 'backoff', 'synced')
                  AND next_run_at <= ?
                  AND NOT EXISTS (
                      SELECT 1
                      FROM page_retirements AS retirement
                      WHERE retirement.product_id = products.product_id
                        AND retirement.resolved_at IS NULL
                  )
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
                RETURNING attempt_id
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
            attempt_id = int(attempt.fetchone()["attempt_id"])

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
                p.reschedule_requested AS current_reschedule_requested,
                p.leased_from_status AS current_leased_from_status,
                p.leased_from_next_run_at
                    AS current_leased_from_next_run_at
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

    def defer_lease(
        self,
        lease_or_token: Lease | str,
        reason: str,
        *,
        now: datetime | None = None,
    ) -> ProductState:
        """Release active work for a product-independent system pause.

        The attempt remains an immutable audit row with the fixed
        ``system_paused`` outcome. The product is restored to the runnable
        queue class and due time captured when it was leased, without changing
        its failure count, publication metadata, or last product outcome.
        """

        if not isinstance(reason, str):
            raise TypeError("reason must be a string")
        normalized_reason = " ".join(reason.split())
        if not normalized_reason:
            raise ValueError("reason must be non-empty")
        if len(normalized_reason) > MAX_SYSTEM_PAUSE_REASON_LENGTH:
            raise ValueError(
                "reason must contain at most "
                f"{MAX_SYSTEM_PAUSE_REASON_LENGTH} characters"
            )

        timestamp = _utc(now)
        now_text = _time_text(timestamp)
        # Persist ordinary expiry recovery before reporting the requested lease
        # as lost; an expired worker must never receive penalty-free deferral.
        self.reclaim_expired_leases(now=timestamp)
        with self._write_transaction() as connection:
            row = self._active_attempt(
                connection,
                lease_or_token,
                timestamp,
            )
            restored_status = row["current_leased_from_status"]
            restored_next_run_at = row["current_leased_from_next_run_at"]
            if restored_status is None or restored_next_run_at is None:
                # Defensive compatibility for a partially upgraded legacy
                # active lease. The v9 migration normally backfills both.
                restored_status = "due"
                restored_next_run_at = now_text
            if restored_status not in {"due", "backoff", "synced"}:
                raise StateError("leased runnable status snapshot is invalid")
            try:
                restored_next_run = _parse_time(restored_next_run_at)
            except (TypeError, ValueError) as exc:
                raise StateError(
                    "leased runnable time snapshot is invalid"
                ) from exc
            if restored_next_run is None:
                raise StateError("leased runnable time snapshot is missing")

            self._mark_started_research_actions_uncertain(
                connection,
                int(row["attempt_id"]),
                finished_at=now_text,
                error="attempt paused before research action completion",
            )
            finished = connection.execute(
                """
                UPDATE attempts
                SET finished_at = ?, outcome = 'system_paused', error = ?
                WHERE attempt_id = ? AND finished_at IS NULL
                """,
                (
                    now_text,
                    normalized_reason,
                    int(row["attempt_id"]),
                ),
            )
            if finished.rowcount != 1:
                raise LeaseLostError("lease attempt was already completed")

            restored = connection.execute(
                """
                UPDATE products
                SET status = ?, next_run_at = ?,
                    lease_token = NULL, lease_owner = NULL, lease_until = NULL,
                    leased_source_hash = NULL, leased_from_status = NULL,
                    leased_from_next_run_at = NULL, reschedule_requested = 0,
                    updated_at = ?
                WHERE product_id = ?
                  AND status = 'leased'
                  AND lease_token = ?
                  AND leased_source_hash = source_hash
                  AND reschedule_requested = 0
                """,
                (
                    restored_status,
                    _time_text(restored_next_run),
                    now_text,
                    row["product_id"],
                    row["lease_token"],
                ),
            )
            if restored.rowcount != 1:
                raise LeaseLostError("lease source changed while it was deferred")
            product_row = connection.execute(
                "SELECT * FROM products WHERE product_id = ?",
                (row["product_id"],),
            ).fetchone()
            if product_row is None:  # defensive; the foreign key owns the attempt
                raise StateError("deferred product disappeared")
            result = self._product_state(product_row)
        return result

    def begin_search(
        self,
        lease_or_token: Lease | str,
        *,
        now: datetime | None = None,
    ) -> datetime:
        """Atomically consume this lease's single Exa search allowance."""

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
                RETURNING action_id
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
                (inserted.fetchone()["action_id"],),
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

    def recent_provider_rejection(
        self,
        provider_fingerprint: str,
        *,
        actions: Iterable[str],
        http_statuses: Iterable[int],
        within: timedelta,
        now: datetime | None = None,
    ) -> ProviderRejectionRecord | None:
        """Return the latest matching provider rejection and exact expiry."""

        normalized_fingerprint = _research_request_fingerprint(
            provider_fingerprint
        )
        normalized_actions = _research_action_filter(actions)
        if not isinstance(within, timedelta) or within <= timedelta(0):
            raise ValueError("within must be a positive timedelta")
        normalized_statuses = _http_status_filter(http_statuses)
        current = _utc(now)
        cutoff = _time_text(current - within)
        action_placeholders = ", ".join("?" for _ in normalized_actions)
        status_placeholders = ", ".join("?" for _ in normalized_statuses)
        parameters: list[Any] = [
            *normalized_actions,
            cutoff,
            _time_text(current),
            normalized_fingerprint,
            *(str(status) for status in normalized_statuses),
        ]
        with self._connection() as connection:
            row = connection.execute(
                f"""
                SELECT
                    action,
                    status,
                    finished_at,
                    result_summary_json
                FROM research_actions
                WHERE action IN ({action_placeholders})
                  AND status IN ('failed', 'uncertain')
                  AND finished_at > ?
                  AND finished_at <= ?
                  AND result_summary_json IS NOT NULL
                  AND CASE
                      WHEN json_valid(result_summary_json)
                      THEN CAST(json_extract(
                          result_summary_json,
                          '$.provider_fingerprint'
                      ) AS TEXT)
                      ELSE NULL
                  END = ?
                  AND CASE
                      WHEN json_valid(result_summary_json)
                      THEN CAST(json_extract(
                          result_summary_json,
                          '$.http_status'
                      ) AS TEXT)
                      ELSE NULL
                  END IN ({status_placeholders})
                ORDER BY finished_at DESC, action_id DESC
                LIMIT 1
                """,
                parameters,
            ).fetchone()
        if row is None:
            return None
        try:
            summary = json.loads(row["result_summary_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise StateError("provider rejection audit is invalid") from exc
        if not isinstance(summary, Mapping):
            raise StateError("provider rejection audit is invalid")
        raw_status = summary.get("http_status")
        if (
            isinstance(raw_status, bool)
            or not isinstance(raw_status, int)
            or raw_status not in normalized_statuses
        ):
            raise StateError("provider rejection HTTP status audit is invalid")
        raw_error_type = summary.get("error_type")
        if raw_error_type is not None and (
            not isinstance(raw_error_type, str)
            or re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,99}", raw_error_type)
            is None
        ):
            raise StateError("provider rejection error type audit is invalid")
        try:
            finished_at = _parse_time(row["finished_at"])
        except (TypeError, ValueError) as exc:
            raise StateError(
                "provider rejection completion time is invalid"
            ) from exc
        if finished_at is None:
            raise StateError("provider rejection completion time is missing")
        return ProviderRejectionRecord(
            action=str(row["action"]),
            research_status=str(row["status"]),
            http_status=raw_status,
            error_type=raw_error_type,
            finished_at=finished_at,
            expires_at=finished_at + within,
        )

    def recent_exa_provider_rejection(
        self,
        provider_fingerprint: str,
        *,
        http_statuses: Iterable[int],
        within: timedelta,
        now: datetime | None = None,
    ) -> ProviderRejectionRecord | None:
        """Return the latest Search/Extract provider-global rejection."""

        return self.recent_provider_rejection(
            provider_fingerprint,
            actions=("search", "extract"),
            http_statuses=http_statuses,
            within=within,
            now=now,
        )

    def recent_provider_error(
        self,
        provider_fingerprint: str,
        *,
        actions: Iterable[str],
        error_types: Iterable[str],
        within: timedelta,
        now: datetime | None = None,
    ) -> ProviderErrorRecord | None:
        """Return the latest matching provider error and exact expiry."""

        normalized_fingerprint = _research_request_fingerprint(
            provider_fingerprint
        )
        normalized_actions = _research_action_filter(actions)
        normalized_error_types = _error_type_filter(error_types)
        if not isinstance(within, timedelta) or within <= timedelta(0):
            raise ValueError("within must be a positive timedelta")
        current = _utc(now)
        action_placeholders = ", ".join("?" for _ in normalized_actions)
        error_placeholders = ", ".join("?" for _ in normalized_error_types)
        parameters: list[Any] = [
            *normalized_actions,
            _time_text(current - within),
            _time_text(current),
            normalized_fingerprint,
            *normalized_error_types,
        ]
        with self._connection() as connection:
            row = connection.execute(
                f"""
                SELECT
                    action,
                    status,
                    finished_at,
                    result_summary_json
                FROM research_actions
                WHERE action IN ({action_placeholders})
                  AND status IN ('failed', 'uncertain')
                  AND finished_at > ?
                  AND finished_at <= ?
                  AND result_summary_json IS NOT NULL
                  AND CASE
                      WHEN json_valid(result_summary_json)
                      THEN CAST(json_extract(
                          result_summary_json,
                          '$.provider_fingerprint'
                      ) AS TEXT)
                      ELSE NULL
                  END = ?
                  AND CASE
                      WHEN json_valid(result_summary_json)
                      THEN CAST(json_extract(
                          result_summary_json,
                          '$.error_type'
                      ) AS TEXT)
                      ELSE NULL
                  END IN ({error_placeholders})
                ORDER BY finished_at DESC, action_id DESC
                LIMIT 1
                """,
                parameters,
            ).fetchone()
        if row is None:
            return None
        try:
            summary = json.loads(row["result_summary_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise StateError("provider error audit is invalid") from exc
        if not isinstance(summary, Mapping):
            raise StateError("provider error audit is invalid")
        raw_error_type = summary.get("error_type")
        if raw_error_type not in normalized_error_types:
            raise StateError("provider error type audit is invalid")
        raw_status = summary.get("http_status")
        if raw_status is not None and (
            isinstance(raw_status, bool)
            or not isinstance(raw_status, int)
            or not 100 <= raw_status <= 599
        ):
            raise StateError("provider error HTTP status audit is invalid")
        try:
            finished_at = _parse_time(row["finished_at"])
        except (TypeError, ValueError) as exc:
            raise StateError(
                "provider error completion time is invalid"
            ) from exc
        if finished_at is None:
            raise StateError("provider error completion time is missing")
        return ProviderErrorRecord(
            action=str(row["action"]),
            research_status=str(row["status"]),
            error_type=raw_error_type,
            http_status=raw_status,
            finished_at=finished_at,
            expires_at=finished_at + within,
        )

    def recent_exa_provider_error(
        self,
        provider_fingerprint: str,
        *,
        error_types: Iterable[str],
        within: timedelta,
        now: datetime | None = None,
    ) -> ProviderErrorRecord | None:
        """Return the latest Search/Extract provider error, with no HTTP need."""

        return self.recent_provider_error(
            provider_fingerprint,
            actions=("search", "extract"),
            error_types=error_types,
            within=within,
            now=now,
        )

    def recent_ai_provider_rejection_event(
        self,
        provider_fingerprint: str,
        *,
        http_statuses: Iterable[int],
        within: timedelta,
        now: datetime | None = None,
    ) -> ProviderRejectionRecord | None:
        """Return an AI rejection with its real completion time and expiry."""

        return self.recent_provider_rejection(
            provider_fingerprint,
            actions=("ai",),
            http_statuses=http_statuses,
            within=within,
            now=now,
        )

    def recent_ai_provider_rejection(
        self,
        provider_fingerprint: str,
        *,
        http_statuses: Iterable[int],
        within: timedelta,
        now: datetime | None = None,
    ) -> int | None:
        """Compatibility accessor returning only the matching AI HTTP status."""

        event = self.recent_ai_provider_rejection_event(
            provider_fingerprint,
            http_statuses=http_statuses,
            within=within,
            now=now,
        )
        return None if event is None else event.http_status

    def recent_ai_provider_error_products(
        self,
        provider_fingerprint: str,
        *,
        error_types: Iterable[str],
        categories: Iterable[str] | None = None,
        within: timedelta,
        now: datetime | None = None,
    ) -> int:
        """Count distinct products with a recent matching AI provider error."""

        normalized_fingerprint = _research_request_fingerprint(
            provider_fingerprint
        )
        if not isinstance(within, timedelta) or within <= timedelta(0):
            raise ValueError("within must be a positive timedelta")
        normalized_error_types = _error_type_filter(error_types)
        normalized_categories: set[str] | None = None
        if categories is not None:
            if isinstance(categories, (str, bytes, bytearray)) or not isinstance(
                categories,
                Iterable,
            ):
                raise TypeError("categories must be an iterable of strings")
            normalized_categories = set()
            for value in categories:
                if not isinstance(value, str):
                    raise TypeError("categories must contain only strings")
                category = value.strip().casefold()
                if category not in _RESEARCH_AI_ERROR_CATEGORIES:
                    raise ValueError(
                        "categories contains an unsupported AI error category"
                    )
                normalized_categories.add(category)
            if not normalized_categories:
                raise ValueError("categories must not be empty")
        current = _utc(now)
        error_placeholders = ", ".join("?" for _ in normalized_error_types)
        clauses = [
            "ra.action = 'ai'",
            "ra.status IN ('failed', 'uncertain')",
            "ra.finished_at > ?",
            "ra.finished_at <= ?",
            "ra.result_summary_json IS NOT NULL",
            """
            CASE
                WHEN json_valid(ra.result_summary_json)
                THEN CAST(json_extract(
                    ra.result_summary_json,
                    '$.provider_fingerprint'
                ) AS TEXT)
                ELSE NULL
            END = ?
            """,
            f"""
            CASE
                WHEN json_valid(ra.result_summary_json)
                THEN CAST(json_extract(
                    ra.result_summary_json,
                    '$.error_type'
                ) AS TEXT)
                ELSE NULL
            END IN ({error_placeholders})
            """,
        ]
        parameters: list[Any] = [
            _time_text(current - within),
            _time_text(current),
            normalized_fingerprint,
            *normalized_error_types,
        ]
        if normalized_categories is not None:
            category_placeholders = ", ".join(
                "?" for _ in normalized_categories
            )
            clauses.append(
                f"""
                CASE
                    WHEN json_valid(ra.result_summary_json)
                    THEN CAST(json_extract(
                        ra.result_summary_json,
                        '$.error_category'
                    ) AS TEXT)
                    ELSE NULL
                END IN ({category_placeholders})
                """
            )
            parameters.extend(sorted(normalized_categories))
        with self._connection() as connection:
            row = connection.execute(
                f"""
                SELECT COUNT(DISTINCT a.product_id) AS count
                FROM research_actions AS ra
                JOIN attempts AS a ON a.attempt_id = ra.attempt_id
                WHERE {' AND '.join(clauses)}
                """,
                parameters,
            ).fetchone()
        return int(row["count"]) if row is not None else 0

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

    def research_usage_between(
        self,
        *,
        start: datetime,
        end: datetime,
    ) -> dict[str, Any]:
        """Return global search/extract spend started in ``[start, end)``.

        Both the current action ledger and legacy one-shot audit columns are
        counted. A missing credit value is reported separately from an
        ``uncertain`` terminal state so a caller can fail closed for either
        condition while still accounting for every known partial credit.
        """

        lower = _strict_utc_datetime(start, name="start")
        upper = _strict_utc_datetime(end, name="end")
        if lower >= upper:
            raise ValueError("start must be earlier than end")
        lower_text = _time_text(lower)
        upper_text = _time_text(upper)

        def empty_bucket() -> dict[str, int | float]:
            return {
                "actions": 0,
                "known_credits": 0.0,
                "unknown_credit_actions": 0,
                "uncertain_actions": 0,
                "unknown_or_uncertain_actions": 0,
            }

        by_action = {
            "search": empty_bucket(),
            "extract": empty_bucket(),
        }

        with self._connection() as connection:
            action_rows = connection.execute(
                """
                SELECT action, status, credits
                FROM research_actions
                WHERE action IN ('search', 'extract')
                  AND started_at >= ?
                  AND started_at < ?
                ORDER BY action_id
                """,
                (lower_text, upper_text),
            ).fetchall()
            legacy_rows = connection.execute(
                """
                SELECT
                    search_started_at,
                    search_usage_json,
                    extract_started_at,
                    extract_usage_json
                FROM attempts
                WHERE (
                    search_started_at >= ? AND search_started_at < ?
                ) OR (
                    extract_started_at >= ? AND extract_started_at < ?
                )
                ORDER BY attempt_id
                """,
                (lower_text, upper_text, lower_text, upper_text),
            ).fetchall()

        for row in action_rows:
            action = str(row["action"])
            bucket = by_action[action]
            bucket["actions"] += 1
            unknown = row["credits"] is None
            uncertain = row["status"] == "uncertain"
            if unknown:
                bucket["unknown_credit_actions"] += 1
            else:
                bucket["known_credits"] += float(row["credits"])
            if uncertain:
                bucket["uncertain_actions"] += 1
            if unknown or uncertain:
                bucket["unknown_or_uncertain_actions"] += 1

        for row in legacy_rows:
            for action in ("search", "extract"):
                started_at = row[f"{action}_started_at"]
                if (
                    started_at is None
                    or started_at < lower_text
                    or started_at >= upper_text
                ):
                    continue
                bucket = by_action[action]
                bucket["actions"] += 1
                usage_json = row[f"{action}_usage_json"]
                if usage_json is None:
                    bucket["unknown_credit_actions"] += 1
                    bucket["unknown_or_uncertain_actions"] += 1
                else:
                    bucket["known_credits"] += _audited_usage_credits(
                        usage_json,
                        name=f"legacy {action}",
                    )

        totals = empty_bucket()
        for bucket in by_action.values():
            for key in totals:
                totals[key] += bucket[key]
        return {
            "start": lower.isoformat(),
            "end": upper.isoformat(),
            **totals,
            "by_action": by_action,
        }

    def allowed_evidence_urls(
        self,
        lease_token: str,
        *,
        now: datetime | None = None,
    ) -> list[str]:
        """Return this active lease's URLs with successfully extracted content."""

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
                    leased_source_hash = NULL, leased_from_status = NULL,
                    leased_from_next_run_at = NULL, reschedule_requested = 0,
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
        content_schema_version: int | None = None,
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
        normalized_content_schema_version = (
            None
            if content_schema_version is None
            else _content_schema_version(content_schema_version, allow_zero=False)
        )
        if not success and normalized_content_schema_version is not None:
            raise ValueError(
                "content_schema_version may be recorded only for a synced outcome"
            )
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
                        leased_source_hash = NULL, leased_from_status = NULL,
                        leased_from_next_run_at = NULL, reschedule_requested = 0,
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
                recorded_content_schema_version = (
                    normalized_content_schema_version
                    if normalized_content_schema_version is not None
                    else int(row["content_schema_version"])
                )
            elif stored_outcome in {
                "tavily_quota_exhausted",
                "search_quota_exhausted",
            }:
                failures = int(row["consecutive_failures"])
                status = "backoff"
                next_run = next_month_start(timestamp)
                last_error = error
                last_success = row["last_success_at"]
                recorded_content_schema_version = int(
                    row["content_schema_version"]
                )
            else:
                failures = int(row["consecutive_failures"]) + 1
                previous_outcomes = connection.execute(
                    """
                    SELECT outcome
                    FROM attempts
                    WHERE product_id = ?
                      AND finished_at IS NOT NULL
                      AND substr(outcome, 1, 7) <> 'system_'
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
                recorded_content_schema_version = int(
                    row["content_schema_version"]
                )

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
                    leased_source_hash = NULL, leased_from_status = NULL,
                    leased_from_next_run_at = NULL, reschedule_requested = 0,
                    last_success_at = ?, last_outcome = ?, last_error = ?,
                    content_schema_version = ?, updated_at = ?
                WHERE product_id = ? AND lease_token = ?
                """,
                (
                    status,
                    _time_text(next_run),
                    failures,
                    last_success,
                    stored_outcome,
                    last_error,
                    recorded_content_schema_version,
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

    def requeue_event_history(
        self,
        product_id: Any | None = None,
        *,
        limit: int = 1000,
    ) -> list[RequeueEventRecord]:
        """Return append-only operator requeue events in creation order."""

        parameters: list[Any] = []
        where = ""
        if product_id is not None:
            product_text = str(product_id).strip()
            if not product_text:
                raise ValueError("product_id must be non-empty")
            where = "WHERE product_id = ?"
            parameters.append(product_text)
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise TypeError("limit must be an integer")
        if not 1 <= limit <= 10_000:
            raise ValueError("limit must be between 1 and 10000")
        parameters.append(limit)
        with self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT *
                FROM requeue_events
                {where}
                ORDER BY event_id
                LIMIT ?
                """,
                parameters,
            ).fetchall()
        return [self._requeue_event_record(row) for row in rows]

    def record_page_retirements(
        self,
        entries: Iterable[PageRetirementInput | Mapping[str, Any]],
        *,
        reason: str,
        backup_sha256: str,
        backup_reference: str | None = None,
        retired_at: datetime | None = None,
    ) -> tuple[PageRetirementRecord, ...]:
        """Atomically tombstone an audited batch of deleted managed pages.

        Each entry declares whether its attempt proves a successful
        publication, a path-bearing publication failure, or only the latest
        durable state of a legacy managed page. Exact retries reuse the active
        audit rows; a conflicting retry fails the whole batch. Active leases
        are reclaimed when expired and otherwise rejected before any row is
        added.
        """

        if isinstance(entries, (str, bytes, bytearray)) or not isinstance(
            entries,
            Iterable,
        ):
            raise TypeError("entries must be an iterable of page retirements")
        normalized_entries = tuple(
            _page_retirement_input(entry) for entry in entries
        )
        if not normalized_entries:
            raise ValueError("entries must not be empty")
        if len(normalized_entries) > MAX_PAGE_RETIREMENT_BATCH:
            raise ValueError(
                f"entries may contain at most {MAX_PAGE_RETIREMENT_BATCH} pages"
            )
        product_ids = [entry.product_id for entry in normalized_entries]
        if len(set(product_ids)) != len(product_ids):
            raise ValueError("entries must not repeat a product_id")
        wiki_paths = [entry.wiki_path for entry in normalized_entries]
        if len(set(wiki_paths)) != len(wiki_paths):
            raise ValueError("entries must not repeat a wiki_path")
        wiki_page_ids = [entry.wiki_page_id for entry in normalized_entries]
        if len(set(wiki_page_ids)) != len(wiki_page_ids):
            raise ValueError("entries must not repeat a wiki_page_id")

        normalized_reason = _page_retirement_reason(reason)
        normalized_backup_sha256 = _sha256_hex(
            backup_sha256,
            name="backup_sha256",
        )
        normalized_backup_reference = _page_retirement_backup_reference(
            backup_reference
            if backup_reference is not None
            else f"sha256:{normalized_backup_sha256}"
        )
        timestamp = _utc(retired_at)
        reclaim_timestamp = _utc()
        retired_at_text = _time_text(timestamp)
        page_updated_texts = {
            entry.product_id: (
                None
                if entry.page_updated_at is None
                else _time_text(entry.page_updated_at)
            )
            for entry in normalized_entries
        }

        with self._write_transaction() as connection:
            self._reclaim_expired(connection, reclaim_timestamp)
            validated: list[
                tuple[PageRetirementInput, Any, Any, Any | None]
            ] = []
            for entry in normalized_entries:
                product_row = connection.execute(
                    """
                    SELECT product_id, source_hash, status,
                           content_schema_version
                    FROM products
                    WHERE product_id = ?
                    """,
                    (entry.product_id,),
                ).fetchone()
                if product_row is None:
                    raise UnknownProductError(entry.product_id)
                if product_row["status"] == "leased":
                    raise StateError(
                        f"cannot retire page for leased product: {entry.product_id}"
                    )

                attempt_row = connection.execute(
                    """
                    SELECT product_id, source_hash, outcome, finished_at,
                           details_json
                    FROM attempts
                    WHERE attempt_id = ?
                    """,
                    (entry.source_attempt_id,),
                ).fetchone()
                if attempt_row is None:
                    raise StateError(
                        "page retirement source attempt does not exist: "
                        f"{entry.source_attempt_id}"
                    )
                if str(attempt_row["product_id"]) != entry.product_id:
                    raise StateError(
                        "page retirement source attempt belongs to another product"
                    )
                if attempt_row["finished_at"] is None:
                    raise StateError(
                        "page retirement source attempt is not finished"
                    )
                if entry.basis == "synced_publication":
                    if attempt_row["outcome"] != "synced":
                        raise StateError(
                            "synced publication retirement requires a synced attempt"
                        )
                    latest_row = connection.execute(
                        """
                        SELECT attempt_id
                        FROM attempts
                        WHERE product_id = ?
                          AND outcome = 'synced'
                          AND finished_at IS NOT NULL
                        ORDER BY attempt_id DESC
                        LIMIT 1
                        """,
                        (entry.product_id,),
                    ).fetchone()
                    if (
                        latest_row is None
                        or int(latest_row["attempt_id"])
                        != entry.source_attempt_id
                    ):
                        raise StateError(
                            "page retirement must reference the latest synced "
                            "attempt"
                        )
                    audited_path = _attempt_wiki_path(
                        attempt_row["details_json"],
                        require_path=True,
                    )
                    if audited_path != entry.wiki_path:
                        raise StateError(
                            "page retirement Wiki path differs from publication "
                            "audit"
                        )
                elif entry.basis == "failed_publication":
                    if (
                        str(attempt_row["outcome"])
                        not in PAGE_RETIREMENT_FAILURE_OUTCOMES
                    ):
                        raise StateError(
                            "failed publication retirement requires a Wiki "
                            "publication failure attempt"
                        )
                    audited_path = _attempt_wiki_path(
                        attempt_row["details_json"],
                        require_path=True,
                    )
                    if audited_path != entry.wiki_path:
                        raise StateError(
                            "page retirement Wiki path differs from publication "
                            "failure audit"
                        )
                    placeholders = ", ".join(
                        "?" for _ in PAGE_RETIREMENT_FAILURE_OUTCOMES
                    )
                    failure_rows = connection.execute(
                        f"""
                        SELECT attempt_id, details_json
                        FROM attempts
                        WHERE product_id = ?
                          AND outcome IN ({placeholders})
                          AND finished_at IS NOT NULL
                        ORDER BY attempt_id DESC
                        """,
                        (
                            entry.product_id,
                            *PAGE_RETIREMENT_FAILURE_OUTCOMES,
                        ),
                    ).fetchall()
                    latest_path_attempt_id: int | None = None
                    for failure_row in failure_rows:
                        if _attempt_wiki_path(
                            failure_row["details_json"],
                            require_path=False,
                        ) is not None:
                            latest_path_attempt_id = int(
                                failure_row["attempt_id"]
                            )
                            break
                    if latest_path_attempt_id != entry.source_attempt_id:
                        raise StateError(
                            "page retirement must reference the latest "
                            "path-bearing publication failure attempt"
                        )
                else:
                    latest_row = connection.execute(
                        """
                        SELECT attempt_id
                        FROM attempts
                        WHERE product_id = ?
                          AND finished_at IS NOT NULL
                        ORDER BY attempt_id DESC
                        LIMIT 1
                        """,
                        (entry.product_id,),
                    ).fetchone()
                    if (
                        latest_row is None
                        or int(latest_row["attempt_id"])
                        != entry.source_attempt_id
                    ):
                        raise StateError(
                            "legacy managed page retirement must reference the "
                            "latest completed attempt"
                        )

                active_row = connection.execute(
                    """
                    SELECT *
                    FROM page_retirements
                    WHERE product_id = ? AND resolved_at IS NULL
                    """,
                    (entry.product_id,),
                ).fetchone()
                if active_row is None:
                    path_owner = connection.execute(
                        """
                        SELECT product_id
                        FROM page_retirements
                        WHERE (wiki_path = ? OR wiki_page_id = ?)
                          AND resolved_at IS NULL
                        """,
                        (entry.wiki_path, entry.wiki_page_id),
                    ).fetchone()
                    if path_owner is not None:
                        raise StateError(
                            "Wiki path or page id is already retired for "
                            "another product"
                        )
                else:
                    expected_page_updated = page_updated_texts[entry.product_id]
                    stored_page_updated = active_row["page_updated_at"]
                    stored_page_updated_text = (
                        None
                        if stored_page_updated is None
                        else _time_text(_parse_time(stored_page_updated))
                    )
                    exact = (
                        str(active_row["basis"]) == entry.basis
                        and int(active_row["source_attempt_id"])
                        == entry.source_attempt_id
                        and str(active_row["source_hash"])
                        == str(attempt_row["source_hash"])
                        and int(active_row["retired_content_schema_version"])
                        == int(product_row["content_schema_version"])
                        and str(active_row["wiki_path"]) == entry.wiki_path
                        and int(active_row["wiki_page_id"]) == entry.wiki_page_id
                        and str(active_row["page_content_sha256"])
                        == entry.page_content_sha256
                        and stored_page_updated_text == expected_page_updated
                        and str(active_row["backup_sha256"])
                        == normalized_backup_sha256
                        and str(active_row["backup_reference"])
                        == normalized_backup_reference
                        and str(active_row["reason"]) == normalized_reason
                    )
                    if not exact:
                        raise StateError(
                            "product already has a conflicting active page retirement"
                        )
                validated.append(
                    (entry, product_row, attempt_row, active_row)
                )

            records: list[PageRetirementRecord] = []
            for entry, product_row, attempt_row, active_row in validated:
                if active_row is None:
                    inserted = connection.execute(
                        """
                        INSERT INTO page_retirements (
                            basis, product_id, source_attempt_id, source_hash,
                            retired_content_schema_version, wiki_path,
                            wiki_page_id, page_content_sha256, page_updated_at,
                            backup_sha256, backup_reference, reason, retired_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        RETURNING *
                        """,
                        (
                            entry.basis,
                            entry.product_id,
                            entry.source_attempt_id,
                            str(attempt_row["source_hash"]),
                            int(product_row["content_schema_version"]),
                            entry.wiki_path,
                            entry.wiki_page_id,
                            entry.page_content_sha256,
                            page_updated_texts[entry.product_id],
                            normalized_backup_sha256,
                            normalized_backup_reference,
                            normalized_reason,
                            retired_at_text,
                        ),
                    )
                    active_row = inserted.fetchone()
                    if active_row is None:
                        raise StateError("page retirement insert returned no row")
                records.append(self._page_retirement_record(active_row))
        return tuple(records)

    def active_page_retirement(
        self,
        product_id: Any,
    ) -> PageRetirementRecord | None:
        """Return the active page tombstone for a product, when present."""

        product_text = str(product_id).strip()
        if not product_text:
            raise ValueError("product_id must be non-empty")
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM page_retirements
                WHERE product_id = ? AND resolved_at IS NULL
                """,
                (product_text,),
            ).fetchone()
        return self._page_retirement_record(row) if row is not None else None

    def page_retirement_history(
        self,
        product_id: Any | None = None,
    ) -> list[PageRetirementRecord]:
        """Return append-only page retirement and resolution history."""

        query = "SELECT * FROM page_retirements"
        parameters: tuple[Any, ...] = ()
        if product_id is not None:
            product_text = str(product_id).strip()
            if not product_text:
                raise ValueError("product_id must be non-empty")
            query += "\nWHERE product_id = ?"
            parameters = (product_text,)
        query += "\nORDER BY retirement_id"
        with self._connection() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [self._page_retirement_record(row) for row in rows]

    def resolve_page_retirement(
        self,
        product_id: Any,
        *,
        reason: str,
        now: datetime | None = None,
    ) -> PageRetirementRecord:
        """Resolve one tombstone and explicitly make its product runnable."""

        product_text = str(product_id).strip()
        if not product_text:
            raise ValueError("product_id must be non-empty")
        normalized_reason = _page_retirement_reason(
            reason,
            name="resolution reason",
        )
        timestamp = _utc(now)
        now_text = _time_text(timestamp)
        with self._write_transaction() as connection:
            self._reclaim_expired(connection, timestamp)
            product_row = connection.execute(
                "SELECT status FROM products WHERE product_id = ?",
                (product_text,),
            ).fetchone()
            if product_row is None:
                raise UnknownProductError(product_text)
            if product_row["status"] == "leased":
                raise StateError(
                    f"cannot resolve page retirement for leased product: {product_text}"
                )
            updated = connection.execute(
                """
                UPDATE page_retirements
                SET resolved_at = ?, resolution_reason = ?
                WHERE product_id = ? AND resolved_at IS NULL
                RETURNING *
                """,
                (now_text, normalized_reason, product_text),
            ).fetchone()
            if updated is None:
                raise StateError("product has no active page retirement")
            connection.execute(
                """
                UPDATE products
                SET status = 'due', next_run_at = ?, updated_at = ?
                WHERE product_id = ? AND status <> 'leased'
                """,
                (now_text, now_text, product_text),
            )
        return self._page_retirement_record(updated)

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
                """
                SELECT
                    CASE
                        WHEN EXISTS (
                            SELECT 1
                            FROM page_retirements AS retirement
                            WHERE retirement.product_id = products.product_id
                              AND retirement.resolved_at IS NULL
                        ) THEN 'retired'
                        ELSE status
                    END AS effective_status,
                    COUNT(*) AS count
                FROM products
                GROUP BY effective_status
                """
            ).fetchall()
        counts = {
            str(row["effective_status"]): int(row["count"])
            for row in rows
        }
        if include_zero:
            return {
                status: counts.get(status, 0)
                for status in EFFECTIVE_PRODUCT_STATUSES
            }
        return counts

    def due_count(self, *, now: datetime | None = None) -> int:
        """Count runnable products without materializing queue payloads."""

        timestamp = _utc(now)
        self.reclaim_expired_leases(now=timestamp)
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM products
                WHERE status IN ('due', 'backoff', 'synced')
                  AND next_run_at <= ?
                  AND NOT EXISTS (
                      SELECT 1
                      FROM page_retirements AS retirement
                      WHERE retirement.product_id = products.product_id
                        AND retirement.resolved_at IS NULL
                  )
                """,
                (_time_text(timestamp),),
            ).fetchone()
        return int(row["count"]) if row is not None else 0

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

    def content_failure_streak(
        self,
        product_id: Any,
        *,
        outcomes: Iterable[str] = CONTENT_FAILURE_OUTCOMES,
        limit: int = 32,
    ) -> int:
        """Count consecutive content failures for the current source revision.

        Unlike per-outcome backoff, alternating content outcomes still count
        toward this streak. The method is read-only and leaves the quarantine
        threshold and scheduling policy to the caller.
        """

        product_text = str(product_id).strip()
        if not product_text:
            raise ValueError("product_id must be non-empty")
        normalized_outcomes = set(
            _outcome_filter(outcomes, name="outcomes")
        )
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise TypeError("limit must be an integer")
        if not 1 <= limit <= 10_000:
            raise ValueError("limit must be between 1 and 10000")

        with self._connection() as connection:
            product_row = connection.execute(
                """
                SELECT source_hash, content_failure_cutoff_attempt_id
                FROM products
                WHERE product_id = ?
                """,
                (product_text,),
            ).fetchone()
            if product_row is None:
                raise UnknownProductError(
                    f"unknown product: {product_text}"
                )
            rows = connection.execute(
                """
                SELECT outcome
                FROM attempts
                WHERE product_id = ?
                  AND source_hash = ?
                  AND attempt_id > ?
                  AND finished_at IS NOT NULL
                  AND outcome IS NOT NULL
                  AND substr(outcome, 1, 7) <> 'system_'
                ORDER BY attempt_id DESC
                LIMIT ?
                """,
                (
                    product_text,
                    product_row["source_hash"],
                    int(product_row["content_failure_cutoff_attempt_id"]),
                    limit,
                ),
            ).fetchall()

        streak = 0
        for row in rows:
            if str(row["outcome"]).casefold() not in normalized_outcomes:
                break
            streak += 1
        return streak

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
                WHERE finished_at IS NOT NULL
                  AND finished_at >= ?
                  AND substr(outcome, 1, 7) <> 'system_'
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
        minimum_content_schema_version: int | None = None,
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
        minimum_version = (
            None
            if minimum_content_schema_version is None
            else _content_schema_version(
                minimum_content_schema_version,
                allow_zero=True,
            )
        )
        query = """
            SELECT
                p.product_id,
                p.content_schema_version,
                p.last_success_at,
                a.attempt_id,
                a.payload_json,
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
              AND NOT EXISTS (
                  SELECT 1
                  FROM page_retirements AS retirement
                  WHERE retirement.product_id = p.product_id
                    AND retirement.resolved_at IS NULL
              )
            ORDER BY p.last_success_at DESC, p.product_id
        """
        parameters_list: list[Any] = []
        if minimum_version is not None:
            query = query.replace(
                "\n            ORDER BY",
                "\n              AND p.content_schema_version >= ?"
                "\n            ORDER BY",
            )
            parameters_list.append(minimum_version)
        if limit is not None:
            query += "\nLIMIT ?"
            parameters_list.append(limit)

        with self._connection() as connection:
            rows = connection.execute(query, tuple(parameters_list)).fetchall()

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
                    source_attempt_id=int(row["attempt_id"]),
                    content_schema_version=int(
                        row["content_schema_version"]
                    ),
                )
            )
        return published

    def content_refresh_candidates(
        self,
        content_schema_version: int,
        *,
        product_ids: Sequence[str] | None = None,
        limit: int = 1000,
    ) -> list[ContentRefreshCandidate]:
        """Return last-synced decisions that need an existing-page refresh.

        The payload is read from the successful attempt, not the current
        catalogue row, so an old verified decision is never combined with a
        newer unverified product revision.
        """

        target_version = _content_schema_version(
            content_schema_version,
            allow_zero=False,
        )
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10_000:
            raise ValueError("limit must be an integer between 1 and 10000")

        normalized_ids: list[str] = []
        if product_ids is not None:
            if isinstance(product_ids, (str, bytes, bytearray)) or not isinstance(
                product_ids,
                Sequence,
            ):
                raise TypeError("product_ids must be a sequence of strings")
            seen_ids: set[str] = set()
            for value in product_ids:
                if not isinstance(value, str):
                    raise TypeError("product_ids must contain only strings")
                product_id = value.strip()
                if not product_id or len(product_id) > 500:
                    raise ValueError(
                        "product_ids must contain bounded non-empty values"
                    )
                if product_id not in seen_ids:
                    seen_ids.add(product_id)
                    normalized_ids.append(product_id)
            if not normalized_ids:
                raise ValueError("product_ids must not be empty")

        query = """
            SELECT
                p.product_id,
                p.content_schema_version,
                a.attempt_id,
                a.source_hash,
                a.payload_json,
                a.finished_at,
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
              AND p.content_schema_version < ?
              AND NOT EXISTS (
                  SELECT 1
                  FROM page_retirements AS retirement
                  WHERE retirement.product_id = p.product_id
                    AND retirement.resolved_at IS NULL
              )
        """
        parameters: list[Any] = [target_version]
        if normalized_ids:
            placeholders = ", ".join("?" for _ in normalized_ids)
            query += f"\nAND p.product_id IN ({placeholders})"
            parameters.extend(normalized_ids)
        query += "\nORDER BY a.finished_at, p.product_id\nLIMIT ?"
        parameters.append(limit)

        with self._connection() as connection:
            rows = connection.execute(query, tuple(parameters)).fetchall()

        candidates: list[ContentRefreshCandidate] = []
        for row in rows:
            try:
                payload_value = json.loads(row["payload_json"])
                details_value = json.loads(row["details_json"])
            except (TypeError, json.JSONDecodeError) as exc:
                raise StateError(
                    "last synced attempt contains invalid refresh audit JSON"
                ) from exc
            if not isinstance(payload_value, Mapping):
                raise StateError(
                    "last synced attempt is missing its product payload"
                )
            if not isinstance(details_value, Mapping):
                raise StateError(
                    "last synced attempt is missing its publication audit"
                )
            recorded_payload = details_value.get("payload")
            if not isinstance(recorded_payload, Mapping):
                raise StateError(
                    "last synced attempt is missing its validated decision"
                )
            decision_value = recorded_payload.get("decision")
            if (
                not isinstance(decision_value, Mapping)
                or decision_value.get("outcome") != "publish"
            ):
                raise StateError(
                    "last synced attempt does not contain a publish decision"
                )
            policy_fingerprint = recorded_payload.get(
                "validation_policy_fingerprint"
            )
            if not isinstance(policy_fingerprint, str) or not policy_fingerprint:
                raise StateError(
                    "last synced attempt lacks a validation policy fingerprint"
                )
            wiki_path_value = details_value.get("wiki_path")
            if (
                not isinstance(wiki_path_value, str)
                or not wiki_path_value.strip("/")
            ):
                raise StateError(
                    "last synced attempt is missing its stable Wiki.js path"
                )
            verified_at = _parse_time(row["finished_at"])
            if verified_at is None:
                raise StateError(
                    "last synced attempt is missing its completion timestamp"
                )

            facts = decision_value.get("facts")
            retained = (
                len(facts)
                if isinstance(facts, Sequence)
                and not isinstance(facts, (str, bytes, bytearray))
                else 0
            )
            raw_diagnostics = recorded_payload.get("fact_diagnostics")
            diagnostics = (
                _fact_diagnostics(raw_diagnostics)
                if isinstance(raw_diagnostics, Mapping)
                else _fact_diagnostics(
                    {
                        "complete": False,
                        "proposed": retained,
                        "retained": retained,
                        "rejected": 0,
                        "rejection_reasons": {},
                    }
                )
            )
            candidates.append(
                ContentRefreshCandidate(
                    product_id=str(row["product_id"]),
                    source_attempt_id=int(row["attempt_id"]),
                    source_hash=str(row["source_hash"]),
                    payload=dict(payload_value),
                    decision=dict(decision_value),
                    wiki_path=wiki_path_value.strip("/"),
                    verified_at=verified_at,
                    previous_content_schema_version=int(
                        row["content_schema_version"]
                    ),
                    fact_diagnostics=diagnostics,
                )
            )
        return candidates

    def record_verified_parameter_set(
        self,
        candidate: ContentRefreshCandidate,
        *,
        pdf_url: str,
        pdf_sha256: str,
        parameters: Sequence[Any],
        extractor_version: str,
        validation_policy_fingerprint: str,
        now: datetime | None = None,
    ) -> VerifiedParameterSetRecord:
        """Persist one canonical datasheet extraction, deduplicated by provenance."""

        if not isinstance(candidate, ContentRefreshCandidate):
            raise TypeError("candidate must be a ContentRefreshCandidate")
        canonical_pdf_url = _canonical_url(pdf_url)
        canonical_pdf_sha256 = _sha256_hex(pdf_sha256, name="pdf_sha256")
        normalized_parameters, parameters_json, parameter_set_sha256 = (
            _verified_parameters(parameters)
        )
        normalized_extractor_version = _bounded_parameter_text(
            extractor_version,
            name="extractor_version",
            max_length=MAX_PARAMETER_VERSION_LENGTH,
        )
        normalized_policy_fingerprint = _sha256_hex(
            validation_policy_fingerprint,
            name="validation_policy_fingerprint",
        )
        timestamp = _utc(now)
        created_at = _time_text(timestamp)

        with self._write_transaction() as connection:
            attempt = connection.execute(
                """
                SELECT product_id, source_hash, outcome, finished_at
                FROM attempts
                WHERE attempt_id = ?
                """,
                (candidate.source_attempt_id,),
            ).fetchone()
            if attempt is None:
                raise StateError("refresh candidate source attempt does not exist")
            if (
                str(attempt["product_id"]) != candidate.product_id
                or str(attempt["source_hash"]) != candidate.source_hash
            ):
                raise StateError(
                    "refresh candidate does not match its source attempt"
                )
            if (
                attempt["outcome"] != "synced"
                or attempt["finished_at"] is None
            ):
                raise StateError(
                    "verified parameters require a completed synced attempt"
                )

            provenance = (
                candidate.product_id,
                candidate.source_attempt_id,
                candidate.source_hash,
                canonical_pdf_url,
                canonical_pdf_sha256,
                parameter_set_sha256,
                normalized_extractor_version,
                normalized_policy_fingerprint,
            )
            row = connection.execute(
                """
                SELECT *
                FROM verified_parameter_sets
                WHERE product_id = ?
                  AND source_attempt_id = ?
                  AND source_hash = ?
                  AND pdf_url = ?
                  AND pdf_sha256 = ?
                  AND parameter_set_sha256 = ?
                  AND extractor_version = ?
                  AND validation_policy_fingerprint = ?
                """,
                provenance,
            ).fetchone()
            if row is None:
                inserted = connection.execute(
                    """
                    INSERT INTO verified_parameter_sets (
                        product_id,
                        source_attempt_id,
                        source_hash,
                        pdf_url,
                        pdf_sha256,
                        parameters_json,
                        parameter_count,
                        parameter_set_sha256,
                        extractor_version,
                        validation_policy_fingerprint,
                        created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    RETURNING *
                    """,
                    (
                        *provenance[:5],
                        parameters_json,
                        len(normalized_parameters),
                        *provenance[5:],
                        created_at,
                    ),
                )
                row = inserted.fetchone()
        if row is None:  # defensive boundary for non-conforming DB adapters
            raise StateError("verified parameter set insert returned no row")
        return self._verified_parameter_set_record(row)

    def get_verified_parameter_set(
        self,
        parameter_set_id: int,
    ) -> VerifiedParameterSetRecord | None:
        """Return a verified parameter set by its durable identifier."""

        normalized_id = _positive_record_id(
            parameter_set_id,
            name="parameter_set_id",
        )
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM verified_parameter_sets
                WHERE parameter_set_id = ?
                """,
                (normalized_id,),
            ).fetchone()
        return (
            self._verified_parameter_set_record(row)
            if row is not None
            else None
        )

    def verified_parameter_set_history(
        self,
        product_id: str | None = None,
        *,
        limit: int = 1000,
    ) -> list[VerifiedParameterSetRecord]:
        """Return append-only verified parameter sets in creation order."""

        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 10_000
        ):
            raise ValueError("limit must be an integer between 1 and 10000")
        query = "SELECT * FROM verified_parameter_sets"
        if product_id is None:
            parameters: tuple[Any, ...] = (limit,)
        else:
            normalized_product_id = str(product_id).strip()
            if not normalized_product_id:
                raise ValueError("product_id must be non-empty")
            query += "\nWHERE product_id = ?"
            parameters = (normalized_product_id, limit)
        query += "\nORDER BY parameter_set_id\nLIMIT ?"
        with self._connection() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [self._verified_parameter_set_record(row) for row in rows]

    def find_completed_parameter_analysis_run(
        self,
        parameter_set_id: int,
        request_fingerprint: str,
    ) -> ParameterAnalysisRunRecord | None:
        """Find a reusable completed run; started/failed rows never qualify."""

        normalized_set_id = _positive_record_id(
            parameter_set_id,
            name="parameter_set_id",
        )
        normalized_fingerprint = _sha256_hex(
            request_fingerprint,
            name="request_fingerprint",
        )
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM parameter_analysis_runs
                WHERE parameter_set_id = ?
                  AND request_fingerprint = ?
                  AND status = 'completed'
                ORDER BY attempt_number DESC
                LIMIT 1
                """,
                (normalized_set_id, normalized_fingerprint),
            ).fetchone()
        return (
            self._parameter_analysis_run_record(row)
            if row is not None
            else None
        )

    def begin_parameter_analysis_run(
        self,
        parameter_set_id: int,
        *,
        request_fingerprint: str,
        prompt_version: str,
        glossary_version: str,
        model: str,
        now: datetime | None = None,
    ) -> ParameterAnalysisRunStartResult:
        """Begin one model attempt unless this exact request is already in flight."""

        normalized_set_id = _positive_record_id(
            parameter_set_id,
            name="parameter_set_id",
        )
        normalized_fingerprint = _sha256_hex(
            request_fingerprint,
            name="request_fingerprint",
        )
        normalized_prompt = _bounded_parameter_text(
            prompt_version,
            name="prompt_version",
            max_length=MAX_PARAMETER_VERSION_LENGTH,
        )
        normalized_glossary = _bounded_parameter_text(
            glossary_version,
            name="glossary_version",
            max_length=MAX_PARAMETER_VERSION_LENGTH,
        )
        normalized_model = _bounded_parameter_text(
            model,
            name="model",
            max_length=MAX_PARAMETER_ANALYSIS_MODEL_LENGTH,
        )
        started_at = _time_text(_utc(now))

        with self._write_transaction() as connection:
            parameter_set = connection.execute(
                """
                SELECT parameter_set_id
                FROM verified_parameter_sets
                WHERE parameter_set_id = ?
                """,
                (normalized_set_id,),
            ).fetchone()
            if parameter_set is None:
                raise StateError(
                    f"unknown verified parameter set: {normalized_set_id}"
                )
            rows = connection.execute(
                """
                SELECT *
                FROM parameter_analysis_runs
                WHERE parameter_set_id = ?
                  AND request_fingerprint = ?
                ORDER BY attempt_number
                """,
                (normalized_set_id, normalized_fingerprint),
            ).fetchall()
            expected_identity = (
                normalized_prompt,
                normalized_glossary,
                normalized_model,
            )
            for row in rows:
                stored_identity = (
                    str(row["prompt_version"]),
                    str(row["glossary_version"]),
                    str(row["model"]),
                )
                if stored_identity != expected_identity:
                    raise StateError(
                        "request_fingerprint was reused with different "
                        "prompt, glossary, or model identity"
                    )
            for reusable_status in ("completed", "started"):
                for row in reversed(rows):
                    if row["status"] == reusable_status:
                        return ParameterAnalysisRunStartResult(
                            record=self._parameter_analysis_run_record(row),
                            should_execute=False,
                        )

            attempt_number = (
                max(int(row["attempt_number"]) for row in rows) + 1
                if rows
                else 1
            )
            inserted = connection.execute(
                """
                INSERT INTO parameter_analysis_runs (
                    parameter_set_id,
                    request_fingerprint,
                    attempt_number,
                    status,
                    prompt_version,
                    glossary_version,
                    model,
                    started_at
                ) VALUES (?, ?, ?, 'started', ?, ?, ?, ?)
                RETURNING *
                """,
                (
                    normalized_set_id,
                    normalized_fingerprint,
                    attempt_number,
                    normalized_prompt,
                    normalized_glossary,
                    normalized_model,
                    started_at,
                ),
            )
            row = inserted.fetchone()
        if row is None:
            raise StateError("parameter analysis insert returned no row")
        return ParameterAnalysisRunStartResult(
            record=self._parameter_analysis_run_record(row),
            should_execute=True,
        )

    def complete_parameter_analysis_run(
        self,
        analysis_run_id: int,
        *,
        analysis: Mapping[str, Any],
        usage: Mapping[str, Any],
        now: datetime | None = None,
    ) -> ParameterAnalysisRunRecord:
        """Complete a started analysis run, with exact terminal replay support."""

        normalized_id = _positive_record_id(
            analysis_run_id,
            name="analysis_run_id",
        )
        _, analysis_json = _parameter_analysis_mapping(
            analysis,
            name="analysis",
            max_bytes=MAX_PARAMETER_ANALYSIS_JSON_BYTES,
        )
        _, usage_json = _parameter_analysis_mapping(
            usage,
            name="usage",
            max_bytes=MAX_PARAMETER_ANALYSIS_USAGE_JSON_BYTES,
        )
        finished_at = _time_text(_utc(now))

        with self._write_transaction() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM parameter_analysis_runs
                WHERE analysis_run_id = ?
                """,
                (normalized_id,),
            ).fetchone()
            if row is None:
                raise StateError(
                    f"unknown parameter analysis run: {normalized_id}"
                )
            if row["status"] == "completed":
                if (
                    row["analysis_json"] == analysis_json
                    and row["usage_json"] == usage_json
                ):
                    return self._parameter_analysis_run_record(row)
                raise StateError(
                    "completed parameter analysis replay has different results"
                )
            if row["status"] != "started":
                raise StateError(
                    "only a started parameter analysis run can complete"
                )
            connection.execute(
                """
                UPDATE parameter_analysis_runs
                SET status = 'completed',
                    analysis_json = ?,
                    usage_json = ?,
                    finished_at = ?
                WHERE analysis_run_id = ? AND status = 'started'
                """,
                (analysis_json, usage_json, finished_at, normalized_id),
            )
            row = connection.execute(
                """
                SELECT *
                FROM parameter_analysis_runs
                WHERE analysis_run_id = ?
                """,
                (normalized_id,),
            ).fetchone()
        if row is None:
            raise StateError("completed parameter analysis run disappeared")
        return self._parameter_analysis_run_record(row)

    def fail_parameter_analysis_run(
        self,
        analysis_run_id: int,
        *,
        error: str,
        usage: Mapping[str, Any] | None = None,
        now: datetime | None = None,
    ) -> ParameterAnalysisRunRecord:
        """Fail a started analysis run while preserving an optional usage audit."""

        normalized_id = _positive_record_id(
            analysis_run_id,
            name="analysis_run_id",
        )
        normalized_error = _parameter_analysis_error(error)
        usage_json = None
        if usage is not None:
            _, usage_json = _parameter_analysis_mapping(
                usage,
                name="usage",
                max_bytes=MAX_PARAMETER_ANALYSIS_USAGE_JSON_BYTES,
            )
        finished_at = _time_text(_utc(now))

        with self._write_transaction() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM parameter_analysis_runs
                WHERE analysis_run_id = ?
                """,
                (normalized_id,),
            ).fetchone()
            if row is None:
                raise StateError(
                    f"unknown parameter analysis run: {normalized_id}"
                )
            if row["status"] == "failed":
                if (
                    row["error"] == normalized_error
                    and row["usage_json"] == usage_json
                ):
                    return self._parameter_analysis_run_record(row)
                raise StateError(
                    "failed parameter analysis replay has different results"
                )
            if row["status"] != "started":
                raise StateError(
                    "only a started parameter analysis run can fail"
                )
            connection.execute(
                """
                UPDATE parameter_analysis_runs
                SET status = 'failed',
                    usage_json = ?,
                    error = ?,
                    finished_at = ?
                WHERE analysis_run_id = ? AND status = 'started'
                """,
                (
                    usage_json,
                    normalized_error,
                    finished_at,
                    normalized_id,
                ),
            )
            row = connection.execute(
                """
                SELECT *
                FROM parameter_analysis_runs
                WHERE analysis_run_id = ?
                """,
                (normalized_id,),
            ).fetchone()
        if row is None:
            raise StateError("failed parameter analysis run disappeared")
        return self._parameter_analysis_run_record(row)

    def parameter_analysis_run_history(
        self,
        parameter_set_id: int,
        *,
        limit: int = 1000,
    ) -> list[ParameterAnalysisRunRecord]:
        """Return every analysis attempt for a verified parameter set."""

        normalized_set_id = _positive_record_id(
            parameter_set_id,
            name="parameter_set_id",
        )
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 10_000
        ):
            raise ValueError("limit must be an integer between 1 and 10000")
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM parameter_analysis_runs
                WHERE parameter_set_id = ?
                ORDER BY analysis_run_id
                LIMIT ?
                """,
                (normalized_set_id, limit),
            ).fetchall()
        return [self._parameter_analysis_run_record(row) for row in rows]

    def record_content_refresh(
        self,
        candidate: ContentRefreshCandidate,
        content_schema_version: int,
        *,
        wiki_action: str,
        fact_diagnostics: Mapping[str, Any],
        parameter_set_id: int | None = None,
        analysis_run_id: int | None = None,
        now: datetime | None = None,
    ) -> ContentRefreshEventRecord:
        """Atomically mark one successful existing-page-only refresh."""

        if not isinstance(candidate, ContentRefreshCandidate):
            raise TypeError("candidate must be a ContentRefreshCandidate")
        target_version = _content_schema_version(
            content_schema_version,
            allow_zero=False,
        )
        action = str(wiki_action).strip().casefold()
        if action not in {"unchanged", "updated"}:
            raise ValueError(
                "content refresh wiki_action must be unchanged or updated"
            )
        diagnostics = _fact_diagnostics(fact_diagnostics)
        normalized_parameter_set_id = (
            None
            if parameter_set_id is None
            else _positive_record_id(
                parameter_set_id,
                name="parameter_set_id",
            )
        )
        normalized_analysis_run_id = (
            None
            if analysis_run_id is None
            else _positive_record_id(
                analysis_run_id,
                name="analysis_run_id",
            )
        )
        if (
            normalized_analysis_run_id is not None
            and normalized_parameter_set_id is None
        ):
            raise ValueError(
                "analysis_run_id requires parameter_set_id"
            )
        timestamp = _utc(now)
        refreshed_at = _time_text(timestamp)

        with self._write_transaction() as connection:
            row = connection.execute(
                """
                SELECT
                    p.content_schema_version,
                    EXISTS (
                        SELECT 1
                        FROM page_retirements AS retirement
                        WHERE retirement.product_id = p.product_id
                          AND retirement.resolved_at IS NULL
                    ) AS page_retired,
                    (
                        SELECT latest.attempt_id
                        FROM attempts AS latest
                        WHERE latest.product_id = p.product_id
                          AND latest.outcome = 'synced'
                          AND latest.finished_at IS NOT NULL
                        ORDER BY latest.attempt_id DESC
                        LIMIT 1
                    ) AS latest_synced_attempt_id
                FROM products AS p
                WHERE p.product_id = ?
                """,
                (candidate.product_id,),
            ).fetchone()
            if row is None:
                raise UnknownProductError(candidate.product_id)
            if bool(row["page_retired"]):
                raise StateError("product page is retired")
            current_version = int(row["content_schema_version"])
            if current_version >= target_version:
                raise StateError(
                    "product content is already at or above the target version"
                )
            if current_version != candidate.previous_content_schema_version:
                raise StateError(
                    "product content version changed during refresh"
                )
            if int(row["latest_synced_attempt_id"]) != candidate.source_attempt_id:
                raise StateError(
                    "a newer synced decision replaced the refresh candidate"
                )

            if normalized_parameter_set_id is not None:
                parameter_set = connection.execute(
                    """
                    SELECT product_id, source_attempt_id, source_hash
                    FROM verified_parameter_sets
                    WHERE parameter_set_id = ?
                    """,
                    (normalized_parameter_set_id,),
                ).fetchone()
                if parameter_set is None:
                    raise StateError(
                        "content refresh parameter set does not exist"
                    )
                if (
                    str(parameter_set["product_id"]) != candidate.product_id
                    or int(parameter_set["source_attempt_id"])
                    != candidate.source_attempt_id
                    or str(parameter_set["source_hash"])
                    != candidate.source_hash
                ):
                    raise StateError(
                        "content refresh parameter set does not belong to "
                        "the candidate"
                    )
            if normalized_analysis_run_id is not None:
                analysis_run = connection.execute(
                    """
                    SELECT parameter_set_id, status
                    FROM parameter_analysis_runs
                    WHERE analysis_run_id = ?
                    """,
                    (normalized_analysis_run_id,),
                ).fetchone()
                if analysis_run is None:
                    raise StateError(
                        "content refresh analysis run does not exist"
                    )
                if (
                    int(analysis_run["parameter_set_id"])
                    != normalized_parameter_set_id
                ):
                    raise StateError(
                        "content refresh analysis run does not belong to "
                        "the parameter set"
                    )
                if analysis_run["status"] != "completed":
                    raise StateError(
                        "content refresh analysis run is not completed"
                    )

            inserted = connection.execute(
                """
                INSERT INTO content_refresh_events (
                    product_id, source_attempt_id,
                    previous_content_schema_version,
                    content_schema_version, wiki_action,
                    fact_diagnostics_json, parameter_set_id,
                    analysis_run_id, refreshed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                RETURNING event_id
                """,
                (
                    candidate.product_id,
                    candidate.source_attempt_id,
                    current_version,
                    target_version,
                    action,
                    _canonical_json(diagnostics),
                    normalized_parameter_set_id,
                    normalized_analysis_run_id,
                    refreshed_at,
                ),
            )
            event_id = int(inserted.fetchone()["event_id"])
            connection.execute(
                """
                UPDATE products
                SET content_schema_version = ?, updated_at = ?
                WHERE product_id = ? AND content_schema_version = ?
                """,
                (
                    target_version,
                    refreshed_at,
                    candidate.product_id,
                    current_version,
                ),
            )
        return ContentRefreshEventRecord(
            event_id=event_id,
            product_id=candidate.product_id,
            source_attempt_id=candidate.source_attempt_id,
            previous_content_schema_version=current_version,
            content_schema_version=target_version,
            wiki_action=action,
            fact_diagnostics=diagnostics,
            parameter_set_id=normalized_parameter_set_id,
            analysis_run_id=normalized_analysis_run_id,
            refreshed_at=timestamp,
        )

    def content_refresh_event_history(
        self,
        product_id: str | None = None,
        *,
        limit: int = 1000,
    ) -> list[ContentRefreshEventRecord]:
        """Return append-only managed-content refresh events."""

        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10_000:
            raise ValueError("limit must be an integer between 1 and 10000")
        parameters: tuple[Any, ...]
        query = "SELECT * FROM content_refresh_events"
        if product_id is None:
            parameters = (limit,)
        else:
            product_id = str(product_id).strip()
            if not product_id:
                raise ValueError("product_id must be non-empty")
            query += "\nWHERE product_id = ?"
            parameters = (product_id, limit)
        query += "\nORDER BY event_id\nLIMIT ?"
        with self._connection() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [
            ContentRefreshEventRecord(
                event_id=int(row["event_id"]),
                product_id=str(row["product_id"]),
                source_attempt_id=int(row["source_attempt_id"]),
                previous_content_schema_version=int(
                    row["previous_content_schema_version"]
                ),
                content_schema_version=int(row["content_schema_version"]),
                wiki_action=str(row["wiki_action"]),
                fact_diagnostics=_fact_diagnostics(
                    json.loads(row["fact_diagnostics_json"])
                ),
                parameter_set_id=(
                    int(row["parameter_set_id"])
                    if row["parameter_set_id"] is not None
                    else None
                ),
                analysis_run_id=(
                    int(row["analysis_run_id"])
                    if row["analysis_run_id"] is not None
                    else None
                ),
                refreshed_at=_parse_time(row["refreshed_at"]),  # type: ignore[arg-type]
            )
            for row in rows
        ]

    @staticmethod
    def _verified_parameter_set_record(
        row: Any,
    ) -> VerifiedParameterSetRecord:
        try:
            stored_parameters = json.loads(row["parameters_json"])
            parameters, _, computed_sha256 = _verified_parameters(
                stored_parameters
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise StateError(
                "stored verified parameter set contains invalid JSON"
            ) from exc
        parameter_count = int(row["parameter_count"])
        parameter_set_sha256 = _sha256_hex(
            row["parameter_set_sha256"],
            name="stored parameter_set_sha256",
        )
        if (
            parameter_count != len(parameters)
            or parameter_set_sha256 != computed_sha256
        ):
            raise StateError(
                "stored verified parameter set failed its canonical digest"
            )
        created_at = _parse_time(row["created_at"])
        if created_at is None:
            raise StateError(
                "stored verified parameter set is missing created_at"
            )
        return VerifiedParameterSetRecord(
            parameter_set_id=int(row["parameter_set_id"]),
            product_id=str(row["product_id"]),
            source_attempt_id=int(row["source_attempt_id"]),
            source_hash=str(row["source_hash"]),
            pdf_url=str(row["pdf_url"]),
            pdf_sha256=_sha256_hex(
                row["pdf_sha256"],
                name="stored pdf_sha256",
            ),
            parameters=parameters,
            parameter_count=parameter_count,
            parameter_set_sha256=parameter_set_sha256,
            extractor_version=str(row["extractor_version"]),
            validation_policy_fingerprint=_sha256_hex(
                row["validation_policy_fingerprint"],
                name="stored validation_policy_fingerprint",
            ),
            created_at=created_at,
        )

    @staticmethod
    def _parameter_analysis_run_record(
        row: Any,
    ) -> ParameterAnalysisRunRecord:
        status = str(row["status"])
        if status not in PARAMETER_ANALYSIS_STATUSES:
            raise StateError(
                f"stored parameter analysis has invalid status: {status}"
            )
        analysis = None
        if row["analysis_json"] is not None:
            try:
                analysis, _ = _parameter_analysis_mapping(
                    json.loads(row["analysis_json"]),
                    name="stored analysis",
                    max_bytes=MAX_PARAMETER_ANALYSIS_JSON_BYTES,
                )
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise StateError(
                    "stored parameter analysis contains invalid analysis JSON"
                ) from exc
        usage = None
        if row["usage_json"] is not None:
            try:
                usage, _ = _parameter_analysis_mapping(
                    json.loads(row["usage_json"]),
                    name="stored usage",
                    max_bytes=MAX_PARAMETER_ANALYSIS_USAGE_JSON_BYTES,
                )
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise StateError(
                    "stored parameter analysis contains invalid usage JSON"
                ) from exc
        started_at = _parse_time(row["started_at"])
        if started_at is None:
            raise StateError(
                "stored parameter analysis is missing started_at"
            )
        return ParameterAnalysisRunRecord(
            analysis_run_id=int(row["analysis_run_id"]),
            parameter_set_id=int(row["parameter_set_id"]),
            request_fingerprint=_sha256_hex(
                row["request_fingerprint"],
                name="stored request_fingerprint",
            ),
            attempt_number=int(row["attempt_number"]),
            status=status,
            prompt_version=str(row["prompt_version"]),
            glossary_version=str(row["glossary_version"]),
            model=str(row["model"]),
            analysis=analysis,
            usage=usage,
            error=(
                str(row["error"])
                if row["error"] is not None
                else None
            ),
            started_at=started_at,
            finished_at=_parse_time(row["finished_at"]),
        )

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
            content_failure_cutoff_attempt_id=int(
                row["content_failure_cutoff_attempt_id"]
            ),
            leased_from_status=row["leased_from_status"],
            leased_from_next_run_at=_parse_time(
                row["leased_from_next_run_at"]
            ),
            content_schema_version=int(row["content_schema_version"]),
        )

    @staticmethod
    def _requeue_event_record(row: sqlite3.Row) -> RequeueEventRecord:
        return RequeueEventRecord(
            event_id=int(row["event_id"]),
            product_id=row["product_id"],
            cutoff_attempt_id=int(row["cutoff_attempt_id"]),
            previous_status=row["previous_status"],
            previous_outcome=row["previous_outcome"],
            reason=row["reason"],
            attempted_after=_parse_time(row["attempted_after"]),
            attempted_before=_parse_time(row["attempted_before"]),
            requeued_at=_parse_time(row["requeued_at"]),  # type: ignore[arg-type]
        )

    @staticmethod
    def _page_retirement_record(row: Any) -> PageRetirementRecord:
        retired_at = _parse_time(row["retired_at"])
        if retired_at is None:
            raise StateError("stored page retirement is missing retired_at")
        return PageRetirementRecord(
            retirement_id=int(row["retirement_id"]),
            basis=_page_retirement_basis(row["basis"]),
            product_id=str(row["product_id"]),
            source_attempt_id=int(row["source_attempt_id"]),
            source_hash=_sha256_hex(
                row["source_hash"],
                name="stored retirement source_hash",
            ),
            retired_content_schema_version=int(
                row["retired_content_schema_version"]
            ),
            wiki_path=_page_retirement_path(row["wiki_path"]),
            wiki_page_id=int(row["wiki_page_id"]),
            page_content_sha256=_sha256_hex(
                row["page_content_sha256"],
                name="stored page_content_sha256",
            ),
            page_updated_at=_parse_time(row["page_updated_at"]),
            backup_sha256=_sha256_hex(
                row["backup_sha256"],
                name="stored backup_sha256",
            ),
            backup_reference=_page_retirement_backup_reference(
                row["backup_reference"]
            ),
            reason=str(row["reason"]),
            retired_at=retired_at,
            resolved_at=_parse_time(row["resolved_at"]),
            resolution_reason=(
                str(row["resolution_reason"])
                if row["resolution_reason"] is not None
                else None
            ),
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
