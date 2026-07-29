"""Command-line orchestration for one bounded PV wiki maintenance cycle."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import math
import os
import re
import socket
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict, is_dataclass, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import __version__
from .ai import (
    AIError,
    AIHTTPError,
    AIInvalidOutputError,
    AILocalExecutionError,
    AIOutputErrorCategory,
    AIResponseError,
    AISettings,
    AITimeoutError,
    FinalAction,
    OpenAICompatibleClient,
    ResearchGap,
    SearchMoreAction,
    TrustedSourcePolicy,
    ValidationFeedback,
    build_parameter_analysis_messages,
)
from .config import (
    ConfigError,
    GlobalResearchBudgetSettings,
    PDFSettings,
    ResearchSettings,
    WikiSettings,
    allow_mirrors,
    include_internal_search_hints,
    max_extract_chars,
    min_fact_confidence,
    min_publish_confidence,
    missing_environment,
    public_brand_alias,
    public_brand_alias_map,
    redact_environment_secrets,
    state_target,
    supplier_public_category,
    supplier_search_excluded_domains,
    trusted_source_domain_map,
    trusted_source_domains_for_product,
)
from .documents import (
    PDF_EXTRACTION_CONTRACT_VERSION,
    PDFDocumentError,
    extract_pdf_evidence,
    looks_like_pdf_url,
)
from .parameter_analysis import (
    PARAMETER_ANALYSIS_PROMPT_VERSION,
    PARAMETER_ANALYSIS_SCHEMA_VERSION,
    PARAMETER_GLOSSARY_VERSION,
    ParameterAnalysisError,
    apply_parameter_translations,
    professional_analysis,
)
from .exa import (
    API_BASE_URL as EXA_API_BASE_URL,
    EXTRACT_CONTRACT_VERSION as EXA_EXTRACT_CONTRACT_VERSION,
    SEARCH_CONTRACT_VERSION as EXA_SEARCH_CONTRACT_VERSION,
    ExaClient,
    ExaError,
    ExaHTTPError,
    ExaQuotaExhaustedError,
    ExaResponseError,
)
from .db import DatabaseConfigurationError, ProductReader, validate_postgres_sslmode
from .decision import (
    DecisionError,
    SourceVerificationError,
    catalogue_model_candidates,
    canonical_product_category,
    identity_key,
    preferred_catalogue_model,
    text_contains_catalogue_identity,
    validate_decision,
)
from .render import (
    manufacturer_display_name,
    render_home_page,
    render_product_page,
    stable_path,
    stable_slug,
)
from .server import WorkerConfigError, WorkerSettings, serve
from .state import (
    AttemptBudgetError,
    ContentRefreshCandidate,
    Lease,
    LeaseLostError,
    StateError,
    StateStore,
    next_month_start,
    research_request_fingerprint,
)
from .search import (
    EXTRACT_BUDGET_UNITS_PER_BATCH,
    SEARCH_BUDGET_UNITS_PER_QUERY,
    build_queries,
)
from .wikijs import WikiJSClient, WikiJSConflictError, WikiJSError


REQUIRED_ENVIRONMENT = (
    "PGHOST",
    "PGPORT",
    "PGDATABASE",
    "PGUSER",
    "PGPASSWORD",
    "PGSSLMODE",
    "WIKIJS_URL",
    "WIKIJS_TOKEN",
)
SECRET_ENVIRONMENT = (
    "PGPASSWORD",
    "EXA_API_KEY",
    "EXA_API_KEYS",
    "WIKIJS_TOKEN",
    "AI_API_KEY",
    "LLM_API_KEY",
    "OPENAI_API_KEY",
    "PV_WIKI_WORKER_TOKEN",
)
MAX_JSON_INPUT_BYTES = 1_000_000
INVALID_DECISION_CIRCUIT_THRESHOLD = 5
INVALID_DECISION_CIRCUIT_WINDOW = timedelta(minutes=30)
MAX_RESEARCH_EVIDENCE_URLS = 5
MAX_RESEARCH_SEARCH_RESULTS = 15
INITIAL_CONTENT_SCHEMA_VERSION = 1
CONTENT_SCHEMA_VERSION = 2
# One AI action may use an initial 300-second call plus one bounded repair.
# A duplicate-create-safe Wiki upsert can require four 120-second requests
# (GET, CREATE, GET, UPDATE), followed by a small scheduling margin.
RESEARCH_LEASE_TAIL_SECONDS = 1200
VALIDATION_POLICY_VERSION = "2026-07-28.5"
AI_RESEARCH_PROMPT_VERSION = "2026-07-28.4"
DEFINITIVE_REJECT_HTTP_STATUSES = frozenset(
    {400, 401, 402, 403, 404, 405, 413, 415, 422, 429}
)
EXA_DEFINITIVE_REJECT_HTTP_STATUSES = DEFINITIVE_REJECT_HTTP_STATUSES
AI_PROVIDER_CIRCUIT_HTTP_STATUSES = frozenset({401, 402, 403, 404})
AI_PROVIDER_CIRCUIT_WINDOW = timedelta(hours=6)
AI_RATE_LIMIT_CIRCUIT_HTTP_STATUSES = frozenset({429})
AI_RATE_LIMIT_CIRCUIT_WINDOW = timedelta(hours=1)
EXA_PROVIDER_CIRCUIT_HTTP_STATUSES = frozenset({401, 403, 404})
EXA_PROVIDER_CIRCUIT_WINDOW = timedelta(hours=6)
EXA_RATE_LIMIT_CIRCUIT_HTTP_STATUSES = frozenset({429})
EXA_RATE_LIMIT_CIRCUIT_WINDOW = timedelta(hours=1)
AI_INVALID_OUTPUT_CIRCUIT_ERROR_TYPES = frozenset(
    {"AIInvalidOutputError", "AIResponseError"}
)
AI_INVALID_OUTPUT_CIRCUIT_CATEGORIES = frozenset(
    {
        AIOutputErrorCategory.ACTION_CONTRACT.value,
        AIOutputErrorCategory.DECISION_CONTRACT.value,
        AIOutputErrorCategory.EMPTY_CONTENT.value,
        AIOutputErrorCategory.INCOMPLETE_RESPONSE.value,
        AIOutputErrorCategory.INVALID_JSON.value,
        AIOutputErrorCategory.PROVIDER_ENVELOPE.value,
    }
)
AI_INVALID_OUTPUT_CIRCUIT_THRESHOLD = 3
AI_INVALID_OUTPUT_CIRCUIT_WINDOW = timedelta(hours=1)
CONTENT_FAILURE_QUARANTINE_THRESHOLD = 6
_AI_TOKEN_USAGE_FIELDS = frozenset(
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
# Catalogue writes may run while paid research is in flight, but the final
# source check, Wiki mutation, and durable outcome must form one in-process
# publication fence. The deployed worker is a single process.
_SOURCE_PUBLISH_FENCE = threading.Lock()
_COMPANY_IDENTIFIER_SUFFIX_RE = re.compile(
    r"(?:srl|ltd|llc|gmbh|bv|inc|corp|company)\Z",
    flags=re.IGNORECASE,
)
_DIRECT_PDF_SUCCESS_STATUSES = frozenset({"used"})
_DIRECT_PDF_PARSED_STATUSES = frozenset({"used", "identity_mismatch"})
_COMMON_COUNTRY_SECOND_LEVELS = frozenset(
    {"ac", "co", "com", "edu", "gov", "net", "org"}
)
_DIRECT_PDF_METADATA_FIELDS = (
    "pdf_direct_status",
    "pdf_direct_error_type",
    "pdf_sha256",
    "pdf_page_count",
    "pdf_extracted_pages",
    "pdf_final_url_changed",
    "pdf_final_hostname",
    "pdf_parameter_rows",
)
_MAX_LOCAL_DATASHEET_PARAMETERS = 200
_MAX_PDF_TARGET_MODELS = 16
_MAX_PDF_TARGET_MODEL_CHARS = 200

_RESEARCH_GAP_NOTES = {
    "manufacturer_identity": (
        "The current extracts do not establish the public manufacturer."
    ),
    "primary_datasheet": (
        "The current extracts do not contain a locally verifiable primary datasheet."
    ),
    "independent_corroboration": (
        "The current extracts lack independent HTTPS corroboration required by "
        "the local source verifier."
    ),
    "missing_exact_fact": (
        "The current extracts do not provide enough exact target-model fact quotes."
    ),
    "conflict_resolution": (
        "The current extracts do not resolve the target model or conflicting claims."
    ),
    "scope_classification": (
        "The current extracts do not reliably establish the component type."
    ),
}


class CLIError(RuntimeError):
    """Raised for an operator-correctable CLI workflow error."""


class ResearchReplaySuppressedError(CLIError):
    """Raised when durable audit state refuses an unsafe paid replay."""


def _normalized_hostname(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    hostname = value.strip().strip(".").casefold()
    if not hostname:
        return ""
    try:
        hostname = hostname.encode("idna").decode("ascii")
    except UnicodeError:
        return ""
    labels = hostname.split(".")
    if (
        len(hostname) > 253
        or any(
            re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
            is None
            for label in labels
        )
    ):
        return ""
    return hostname


def _url_hostname(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    try:
        return _normalized_hostname(urlsplit(value).hostname)
    except ValueError:
        return ""


def _registrable_domain(hostname: Any) -> str:
    normalized = _normalized_hostname(hostname)
    if not normalized:
        return ""
    try:
        ipaddress.ip_address(normalized)
    except ValueError:
        pass
    else:
        return ""
    labels = normalized.split(".")
    if len(labels) < 2:
        return ""
    label_count = 2
    if (
        len(labels) >= 3
        and len(labels[-1]) == 2
        and labels[-2] in _COMMON_COUNTRY_SECOND_LEVELS
    ):
        label_count = 3
    return ".".join(labels[-label_count:])


def _hostname_matches_domain(hostname: Any, domain: Any) -> bool:
    normalized_hostname = _normalized_hostname(hostname)
    normalized_domain = _normalized_hostname(domain)
    return bool(
        normalized_hostname
        and normalized_domain
        and (
            normalized_hostname == normalized_domain
            or normalized_hostname.endswith(f".{normalized_domain}")
        )
    )


def _pdf_redirect_allows_publication(
    requested_url: str,
    final_hostname: Any,
    *,
    trusted_domains: frozenset[str],
) -> bool:
    requested_hostname = _url_hostname(requested_url)
    normalized_final_hostname = _normalized_hostname(final_hostname)
    requested_domain = _registrable_domain(requested_hostname)
    final_domain = _registrable_domain(normalized_final_hostname)
    if (
        requested_domain
        and final_domain
        and requested_domain == final_domain
    ):
        return True
    return any(
        _hostname_matches_domain(normalized_final_hostname, domain)
        for domain in trusted_domains
    )


def _json_default(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"{type(value).__name__} is not JSON serializable")


def _emit(value: Any, *, stream: Any = None) -> None:
    print(
        json.dumps(value, ensure_ascii=False, sort_keys=True, default=_json_default),
        file=stream or sys.stdout,
    )


def _safe_error(error: BaseException) -> str:
    message = str(error) or error.__class__.__name__
    return redact_environment_secrets(
        message,
        SECRET_ENVIRONMENT,
        limit=2000,
    )


def _read_json(path_value: str) -> dict[str, Any]:
    path = Path(path_value).expanduser()
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise CLIError(f"cannot read JSON file: {path}") from exc
    if size > MAX_JSON_INPUT_BYTES:
        raise CLIError(f"JSON file exceeds {MAX_JSON_INPUT_BYTES} bytes")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CLIError(f"invalid JSON file: {path}") from exc
    if not isinstance(value, dict):
        raise CLIError("JSON input must be an object")
    return value


def _read_evidence_texts(path_value: str) -> dict[str, str]:
    payload = _read_json(path_value)
    container = payload.get("extract", payload)
    if not isinstance(container, dict):
        raise CLIError("evidence file must contain an extract object")
    results = container.get("results")
    if not isinstance(results, list) or len(results) > 5:
        raise CLIError("evidence file must contain at most 5 extract results")
    evidence: dict[str, str] = {}
    for item in results:
        if not isinstance(item, dict):
            raise CLIError("evidence extract results must be objects")
        url = item.get("url")
        body = item.get("raw_content")
        if not isinstance(url, str) or not isinstance(body, str) or not body.strip():
            continue
        if url in evidence and evidence[url] != body:
            raise CLIError("evidence file has duplicate URLs with different content")
        evidence[url] = body
    return evidence


def _store() -> StateStore:
    return StateStore(state_target())


def _require_lease(store: StateStore, token: str) -> Lease:
    lease = store.get_by_lease(token)
    if lease is None:
        raise LeaseLostError("lease is missing, expired, or already completed")
    check = store.precheck(lease)
    if not check.ready:
        raise LeaseLostError(f"lease cannot continue: {check.reason}")
    return lease


def _search_identity(product: dict[str, Any]) -> dict[str, Any]:
    """Map known catalogue columns to provider-neutral identity aliases."""

    public_name = str(product.get("product_name") or "").strip()
    include_internal_hints = include_internal_search_hints()
    stable_id = " ".join(str(product.get("product_id") or "").split())
    public_id_approved = bool(
        include_internal_hints
        and 3 <= len(stable_id) <= 200
        and not any(ord(character) < 32 for character in stable_id)
        and "://" not in stable_id
        and not _COMPANY_IDENTIFIER_SUFFIX_RE.search(stable_id)
    )
    catalogue_candidates = catalogue_model_candidates(
        stable_id,
        public_name,
        allow_product_id=public_id_approved,
    )
    non_description_candidates = tuple(
        candidate
        for candidate in catalogue_candidates
        if not public_name
        or candidate.casefold() != public_name.casefold()
    )
    model_candidates = non_description_candidates or catalogue_candidates
    public_model = model_candidates[0] if model_candidates else ""
    if not public_model:
        raise CLIError(
            "catalogue identity is empty; record insufficient_identity without search"
        )
    identity: dict[str, Any] = {"model": public_model}
    if len(model_candidates) > 1:
        identity["model_candidates"] = list(model_candidates)
    if public_name and public_name != public_model:
        identity["product_name"] = public_name
    if include_internal_hints:
        brand = str(product.get("brand_code") or "").strip()
        manufacturer_alias = public_brand_alias(brand)
        if manufacturer_alias:
            identity["manufacturer"] = manufacturer_alias
        public_category = supplier_public_category(brand)
        if public_category:
            identity["category"] = public_category
        search_domains = trusted_source_domains_for_product(
            brand,
            manufacturer_alias or None,
        )
        if search_domains:
            identity["_search_domains"] = sorted(search_domains)
    return identity


def _trusted_source_policy_for_product(
    product: Mapping[str, Any],
) -> TrustedSourcePolicy | None:
    """Return only public operator-owned supplier context for the AI prompt."""

    brand = str(product.get("brand_code") or "").strip()
    manufacturer = public_brand_alias(brand)
    domains = trusted_source_domains_for_product(
        brand,
        manufacturer or None,
    )
    if not manufacturer or not domains:
        return None
    return TrustedSourcePolicy(
        manufacturer=manufacturer,
        domains=tuple(sorted(domains)),
    )


def _make_search_client(*, timeout: float) -> ExaClient:
    return ExaClient(timeout=timeout)


def _client_provider(client: Any) -> str:
    del client
    return "exa"


def _provider_quota_error(exc: BaseException) -> bool:
    return isinstance(exc, ExaQuotaExhaustedError)


def _provider_error(exc: BaseException) -> bool:
    return isinstance(exc, ExaError)


def _provider_outcome(exc: BaseException, client: Any) -> str:
    del client
    if _provider_quota_error(exc):
        return "search_quota_exhausted"
    if _provider_error(exc):
        return "search_error"
    return "error"


def _credits(bundle: Mapping[str, Any]) -> float:
    usage = bundle.get("usage")
    if not isinstance(usage, Mapping) or "credits" not in usage:
        raise ExaResponseError(
            "Exa result is missing explicit usage.credits"
        )
    raw = usage["credits"]
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ExaResponseError(
            "Exa usage.credits must be a finite non-negative number"
        )
    value = float(raw)
    if not math.isfinite(value) or value < 0:
        raise ExaResponseError(
            "Exa usage.credits must be a finite non-negative number"
        )
    return value


def _usage_cost_dollars(bundle: Mapping[str, Any]) -> float:
    usage = bundle.get("usage")
    if not isinstance(usage, Mapping):
        return 0.0
    raw = usage.get("cost_dollars")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return 0.0
    value = float(raw)
    return value if math.isfinite(value) and value >= 0 else 0.0


def _merged_usage(
    current: Mapping[str, Any] | None,
    addition: Mapping[str, Any],
) -> dict[str, int | float]:
    usage: dict[str, int | float] = {
        "credits": (
            (0.0 if not current else _credits(current))
            + _credits(addition)
        )
    }
    cost_dollars = (
        (0.0 if not current else _usage_cost_dollars(current))
        + _usage_cost_dollars(addition)
    )
    if cost_dollars:
        usage["cost_dollars"] = round(cost_dollars, 9)
    return usage


def _merge_search_bundles(
    current: Mapping[str, Any] | None,
    addition: Mapping[str, Any],
) -> dict[str, Any]:
    """Merge bounded Exa search batches without trusting snippets as evidence."""

    queries: list[str] = []
    seen_queries: set[str] = set()
    for bundle in (current or {}, addition):
        raw_queries = bundle.get("queries")
        if not isinstance(raw_queries, list):
            continue
        for query in raw_queries:
            if not isinstance(query, str):
                continue
            key = query.casefold()
            if query and key not in seen_queries:
                seen_queries.add(key)
                queries.append(query[:400])

    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for bundle in (current or {}, addition):
        results = bundle.get("results")
        if not isinstance(results, list):
            continue
        for item in results:
            if not isinstance(item, Mapping):
                continue
            url = item.get("url")
            if not isinstance(url, str) or not url:
                continue
            candidate = dict(item)
            existing = merged.get(url)
            if existing is None:
                order.append(url)
                merged[url] = candidate
                continue
            old_score = existing.get("score")
            new_score = candidate.get("score")
            if (
                isinstance(new_score, (int, float))
                and not isinstance(new_score, bool)
                and (
                    not isinstance(old_score, (int, float))
                    or isinstance(old_score, bool)
                    or float(new_score) > float(old_score)
                )
            ):
                merged[url] = candidate
            source_queries: list[str] = []
            for source in (existing, candidate):
                raw_sources = source.get("source_queries")
                if not isinstance(raw_sources, list):
                    continue
                for query in raw_sources:
                    if isinstance(query, str) and query not in source_queries:
                        source_queries.append(query[:400])
            if source_queries:
                merged[url]["source_queries"] = source_queries

    request_ids: list[str] = []
    for bundle in (current or {}, addition):
        raw_ids = bundle.get("request_ids")
        if isinstance(raw_ids, list):
            for request_id in raw_ids:
                if (
                    isinstance(request_id, str)
                    and request_id
                    and request_id not in request_ids
                ):
                    request_ids.append(request_id[:300])
    provider = str(
        addition.get("provider")
        or (current or {}).get("provider")
        or "exa"
    )
    return {
        "provider": provider,
        "queries": queries,
        "search_depth": str(addition.get("search_depth") or "basic")[:40],
        "results": [
            merged[url]
            for url in order[:MAX_RESEARCH_SEARCH_RESULTS]
        ],
        "usage": _merged_usage(current, addition),
        "request_ids": request_ids,
    }


def _merge_extract_bundles(
    current: Mapping[str, Any] | None,
    addition: Mapping[str, Any],
) -> dict[str, Any]:
    """Merge successful extract bodies while preserving the five-URL hard cap."""

    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for bundle in (current or {}, addition):
        results = bundle.get("results")
        if not isinstance(results, list):
            continue
        for item in results:
            if not isinstance(item, Mapping):
                continue
            url = item.get("url")
            if not isinstance(url, str) or not url:
                continue
            candidate = dict(item)
            existing = merged.get(url)
            if existing is None:
                order.append(url)
                merged[url] = candidate
                continue
            old_body = existing.get("raw_content")
            new_body = candidate.get("raw_content")
            old_identity = existing.get("identity_verified") is True
            new_identity = candidate.get("identity_verified") is True
            prefer_candidate = isinstance(new_body, str) and (
                not isinstance(old_body, str)
                or (new_identity and not old_identity)
                or (
                    new_identity == old_identity
                    and len(new_body) > len(old_body)
                )
            )
            selected = candidate if prefer_candidate else existing
            other = existing if prefer_candidate else candidate
            combined = dict(other)
            combined.update(selected)

            def metadata_rank(value: Mapping[str, Any]) -> int:
                status = value.get("pdf_direct_status")
                if status == "used":
                    return 4
                if status == "identity_mismatch":
                    return 3
                if status == "failed":
                    return 2
                if status == "attempted":
                    return 1
                return 0

            for field in _DIRECT_PDF_METADATA_FIELDS:
                combined.pop(field, None)
            for source in sorted(
                (existing, candidate),
                key=metadata_rank,
            ):
                for field in _DIRECT_PDF_METADATA_FIELDS:
                    if field in source:
                        combined[field] = source[field]
            if combined.get("pdf_direct_status") in _DIRECT_PDF_PARSED_STATUSES:
                combined.pop("pdf_direct_error_type", None)
            merged[url] = combined

    failed: list[dict[str, Any]] = []
    seen_failed: set[tuple[str, str]] = set()
    for bundle in (current or {}, addition):
        raw_failed = bundle.get("failed_results")
        if not isinstance(raw_failed, list):
            continue
        for item in raw_failed:
            if not isinstance(item, Mapping):
                continue
            key = (str(item.get("url") or ""), str(item.get("error") or ""))
            if key not in seen_failed:
                seen_failed.add(key)
                failed.append(dict(item))
    query = addition.get("query")
    if not isinstance(query, str):
        query = (current or {}).get("query")
    provider = str(
        addition.get("provider")
        or (current or {}).get("provider")
        or "exa"
    )
    return {
        "provider": provider,
        "query": str(query or "")[:400],
        "results": [
            merged[url]
            for url in order[:MAX_RESEARCH_EVIDENCE_URLS]
        ],
        "failed_results": failed[:10],
        "usage": _merged_usage(current, addition),
    }


def _validation_research_gap(error: DecisionError) -> str | None:
    """Map only known evidence deficiencies to another bounded research round."""

    message = str(error).casefold()
    if isinstance(error, SourceVerificationError):
        if "primary datasheet" in message:
            return "primary_datasheet"
        return "independent_corroboration"
    if "requires a discovered manufacturer" in message:
        return "manufacturer_identity"
    if "requires a primary datasheet" in message:
        return "primary_datasheet"
    if "facts[" in message and (
        "evidence_quotes" in message
        or "exact supporting extract span" in message
    ):
        return "missing_exact_fact"
    if (
        "model does not exactly match" in message
        or "publish model is not bound" in message
    ):
        return "conflict_resolution"
    if "cited publish evidence" in message:
        return "missing_exact_fact"
    if "generic hardware" in message:
        return "scope_classification"
    if "out_of_scope" in message and (
        "classification" in message or "identity" in message
    ):
        return "scope_classification"
    if "classification_evidence_quotes" in message:
        return "scope_classification"
    return None


def _worker_id(explicit: str | None) -> str:
    if explicit and explicit.strip():
        return explicit.strip()[:200]
    return f"pv-wiki:{socket.gethostname()[:80]}:{os.getpid()}"


def _tag_slug(prefix: str, value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return f"{prefix}-{stable_slug(text, max_length=64)}"
    except ValueError:
        return None


def _wiki_tags(product: dict[str, Any], decision: dict[str, Any]) -> list[str]:
    tags = {"product", "datasheet-found", "managed-by-pv-wiki"}
    for item in decision["datasheets"] + decision["sources"]:
        tags.add(f"source-{item['source_type']}")
    brand_tag = _tag_slug(
        "brand",
        manufacturer_display_name(product, decision),
    )
    if brand_tag:
        tags.add(brand_tag)
    category_tag = _tag_slug(
        "category", _decision_product_category(decision)
    )
    if category_tag:
        tags.add(category_tag)
    return sorted(tags)


def _decision_product_category(decision: dict[str, Any]) -> str:
    """Read the public category, including a narrow legacy-fact fallback."""

    value = decision.get("product_category")
    if isinstance(value, str) and value.strip():
        return canonical_product_category(value)
    for fact in decision.get("facts", []):
        if not isinstance(fact, dict):
            continue
        name = str(fact.get("name") or "").strip().casefold()
        if name not in {
            "product category",
            "product type",
            "产品类别",
            "产品类型",
        }:
            continue
        legacy_value = fact.get("value")
        if isinstance(legacy_value, str) and legacy_value.strip():
            return canonical_product_category(legacy_value)
    return ""


def _decision_for_render(decision: Mapping[str, Any]) -> dict[str, Any]:
    """Upgrade historical display fields without re-authorizing the decision."""

    rendered = dict(decision)
    raw_category = decision.get("product_category")
    category = _decision_product_category(rendered)
    if category:
        rendered["product_category"] = category
    if (
        not str(rendered.get("product_type") or "").strip()
        and isinstance(raw_category, str)
        and raw_category.strip()
        and raw_category.strip() != category
    ):
        rendered["product_type"] = raw_category.strip()
    return rendered


def _home_catalogue_product(item: Any, *, path_prefix: str) -> dict[str, Any]:
    payload = item.payload
    decision = item.decision
    return {
        "product_id": item.product_id,
        "product_name": payload.get("product_name"),
        "model": item.product_id,
        "display_title_zh": decision.get("display_title_zh"),
        "product_description_zh": (
            decision.get("product_description_zh")
            or decision.get("display_title_zh")
        ),
        "manufacturer": manufacturer_display_name(payload, decision),
        "product_category": _decision_product_category(decision),
        "wiki_path": item.wiki_path or stable_path(payload, prefix=path_prefix),
        "published_at": item.published_at,
    }


def _verified_wiki_page_fields(
    product_id: str,
    payload: Mapping[str, Any],
    decision: Mapping[str, Any],
    *,
    checked_at: datetime,
) -> dict[str, Any]:
    """Render Wiki fields from an already validated publish decision."""

    clean_product_id = " ".join(str(product_id).split())
    if not clean_product_id:
        raise CLIError("verified product is missing its product ID")
    render_decision = _decision_for_render(decision)
    product = {
        **dict(payload),
        "manufacturer": manufacturer_display_name(payload, render_decision),
        "model": render_decision.get("model") or payload.get("product_name"),
    }
    summary = " ".join(str(render_decision.get("summary") or "").split())
    return {
        # Wiki metadata and the catalogue's primary link label are stable IDs.
        "title": clean_product_id,
        "description": (
            summary
            or f"Datasheet and cited specifications for {clean_product_id}."
        )[:255],
        "managed_content": render_product_page(
            product,
            render_decision,
            checked_at,
        ),
        "tags": _wiki_tags(dict(payload), render_decision),
    }


def _cmd_doctor(args: argparse.Namespace) -> int:
    missing = missing_environment(REQUIRED_ENVIRONMENT)
    provider_keys = os.getenv("EXA_API_KEYS", "").strip()
    provider_single = os.getenv("EXA_API_KEY", "").strip()
    if not provider_keys and not provider_single:
        missing.append("EXA_API_KEYS or EXA_API_KEY")
    provider_check = {
        "ok": True,
        "provider": "exa",
        "note": "configuration only; doctor does not spend search credits",
    }
    state = _store()
    checks: dict[str, Any] = {
        "state": {
            "ok": True,
            "backend": state.backend,
            "location": state.location,
            "schema_version": state.schema_version,
        },
        "environment": {"ok": not missing, "missing": missing},
        "search_provider_config": provider_check,
        "live": bool(args.live),
    }
    state.close()

    wiki_settings: WikiSettings | None = None
    try:
        wiki_settings = WikiSettings.from_env()
        checks["wikijs_config"] = {"ok": True}
    except ConfigError as exc:
        checks["wikijs_config"] = {"ok": False, "error": _safe_error(exc)}

    public_alias_mapping: dict[str, str] | None = None
    try:
        public_alias_mapping = public_brand_alias_map()
        checks["public_brand_aliases_config"] = {
            "ok": True,
            "brand_mappings": len(public_alias_mapping),
        }
    except ConfigError as exc:
        checks["public_brand_aliases_config"] = {
            "ok": False,
            "error": _safe_error(exc),
        }

    trusted_domain_mapping: dict[str, frozenset[str]] | None = None
    try:
        trusted_domain_mapping = trusted_source_domain_map()
        checks["trusted_sources_config"] = {
            "ok": True,
            "brand_mappings": len(trusted_domain_mapping),
            "domains": sum(
                len(domains) for domains in trusted_domain_mapping.values()
            ),
        }
    except ConfigError as exc:
        checks["trusted_sources_config"] = {
            "ok": False,
            "error": _safe_error(exc),
        }

    research_settings: ResearchSettings | None = None
    try:
        research_settings = ResearchSettings.from_env()
        checks["research_config"] = {
            "ok": True,
            "max_rounds": research_settings.max_rounds,
            "max_queries": research_settings.max_queries,
            "max_credits": research_settings.max_credits,
            "max_seconds": research_settings.max_seconds,
        }
    except ConfigError as exc:
        checks["research_config"] = {
            "ok": False,
            "error": _safe_error(exc),
        }

    pdf_settings: PDFSettings | None = None
    try:
        pdf_settings = PDFSettings.from_env()
        checks["pdf_document_config"] = {
            "ok": True,
            "enabled": pdf_settings.enabled,
            "transport": "pinned-public-https",
            "parser": "isolated-pypdf",
            "max_files": pdf_settings.max_files,
            "max_bytes": pdf_settings.max_bytes,
            "max_pages": pdf_settings.max_pages,
            "download_timeout_seconds": pdf_settings.download_timeout,
            "parse_timeout_seconds": pdf_settings.parse_timeout,
        }
    except ConfigError as exc:
        checks["pdf_document_config"] = {
            "ok": False,
            "error": _safe_error(exc),
        }

    global_budget_settings: GlobalResearchBudgetSettings | None = None
    try:
        global_budget_settings = GlobalResearchBudgetSettings.from_env()
        if research_settings is not None:
            _validate_global_research_limits(
                global_budget_settings,
                research_settings.max_credits,
            )
        checks["global_research_budget_config"] = {
            "ok": True,
            "daily_credit_limit":
                global_budget_settings.daily_credit_limit,
            "monthly_credit_limit":
                global_budget_settings.monthly_credit_limit,
            "counts": "exa_search_extract_units_only",
        }
    except (CLIError, ConfigError) as exc:
        checks["global_research_budget_config"] = {
            "ok": False,
            "error": _safe_error(exc),
        }

    ai_settings: AISettings | None = None
    try:
        ai_settings = AISettings.from_env()
        checks["ai_config"] = {
            "ok": True,
            "base_url": ai_settings.base_url,
            "model": ai_settings.model,
            "thinking_mode": (
                ai_settings.thinking_mode or "provider_default"
            ),
            "reasoning_effort": (
                ai_settings.reasoning_effort or "provider_default"
            ),
            "note": "configuration validated; doctor does not spend AI tokens",
        }
    except AIError as exc:
        checks["ai_config"] = {"ok": False, "error": _safe_error(exc)}

    pg_sslmode: str | None = None
    try:
        pg_sslmode = validate_postgres_sslmode()
        checks["postgresql_tls"] = {
            "ok": True,
            "sslmode": pg_sslmode,
            "recommended": "verify-full",
        }
        if pg_sslmode in {"disable", "allow", "prefer"}:
            checks["postgresql_tls"]["warning"] = (
                "catalogue transport may be unencrypted; use this mode only "
                "after the operator accepts the network risk"
            )
    except DatabaseConfigurationError as exc:
        checks["postgresql_tls"] = {"ok": False, "error": _safe_error(exc)}

    if (
        args.live
        and not missing
        and wiki_settings is not None
        and ai_settings is not None
        and public_alias_mapping is not None
        and trusted_domain_mapping is not None
        and research_settings is not None
        and pdf_settings is not None
        and global_budget_settings is not None
        and pg_sslmode
    ):
        generator = ProductReader().iter_products(batch_size=1)
        try:
            first = next(generator, None)
        finally:
            generator.close()
        checks["postgresql"] = {
            "ok": True,
            "read_only_transaction_enforced": True,
            "sslmode": pg_sslmode,
            "sample_product": first is not None,
        }
        client = WikiJSClient(
            wiki_settings.base_url,
            wiki_settings.token,
            timeout=wiki_settings.timeout,
            new_page_private=wiki_settings.new_page_private,
            new_page_published=wiki_settings.new_page_published,
        )
        probe_path = f"{wiki_settings.path_prefix}/__pv-wiki-doctor__"
        try:
            client.get_page(probe_path, wiki_settings.locale)
        except WikiJSError as exc:
            if "does not exist" in str(exc).casefold():
                pass  # Page not found is expected — API is reachable.
            else:
                raise
        checks["wikijs"] = {"ok": True, "read_probe": probe_path}
        checks["search_provider"] = {
            "ok": True,
            "provider": "exa",
            "note": "key configured; no credits spent by doctor",
        }

    ok = (
        checks["environment"]["ok"]
        and checks["wikijs_config"]["ok"]
        and all(
            value.get("ok", True)
            for value in checks.values()
            if isinstance(value, dict)
        )
    )
    _emit({"ok": ok, "checks": checks})
    return 0 if ok else 2


def _cmd_sync_db(_args: argparse.Namespace) -> int:
    _emit(sync_catalogue())
    return 0


def _product_source_value(product: Any, field: str) -> Any:
    if isinstance(product, Mapping):
        return product.get(field)
    return getattr(product, field, None)


def _inherit_registered_sibling_brands(
    products: list[Any],
) -> tuple[list[Any], int]:
    """Fill a missing brand from one exact, branded catalogue model sibling.

    Bundle rows such as ``KIT H1-4K-S2`` may omit ``brand_code`` even though
    the same read-only catalogue contains the exact base product
    ``H1-4K-S2`` with ``brand_code=SAJ``. Only an exact model-candidate match
    to one registered supplier is accepted; fuzzy names and conflicting
    brands remain empty for AI/local verification.
    """

    brands_by_product_id: dict[str, set[str]] = {}
    for product in products:
        product_id = str(
            _product_source_value(product, "product_id") or ""
        ).strip()
        brand = str(
            _product_source_value(product, "brand_code") or ""
        ).strip()
        if not product_id or not brand or not public_brand_alias(brand):
            continue
        brands_by_product_id.setdefault(
            product_id.casefold(),
            set(),
        ).add(brand)

    enriched: list[Any] = []
    inferred = 0
    for product in products:
        existing_brand = str(
            _product_source_value(product, "brand_code") or ""
        ).strip()
        if existing_brand:
            enriched.append(product)
            continue
        product_id = str(
            _product_source_value(product, "product_id") or ""
        ).strip()
        product_name = str(
            _product_source_value(product, "product_name") or ""
        ).strip()
        matched_brands = {
            brand
            for candidate in catalogue_model_candidates(
                product_id,
                product_name,
                allow_product_id=True,
            )
            for brand in brands_by_product_id.get(
                candidate.casefold(),
                set(),
            )
        }
        if len(matched_brands) != 1:
            enriched.append(product)
            continue
        inferred_brand = next(iter(matched_brands))
        if isinstance(product, Mapping):
            updated = dict(product)
            updated["brand_code"] = inferred_brand
        elif is_dataclass(product) and not isinstance(product, type):
            updated = replace(product, brand_code=inferred_brand)
        else:
            enriched.append(product)
            continue
        enriched.append(updated)
        inferred += 1
    return enriched, inferred


def sync_catalogue(*, resume_quota: bool = True) -> dict[str, Any]:
    """Refresh the queue, optionally waking explicit monthly quota waits."""

    if not isinstance(resume_quota, bool):
        raise TypeError("resume_quota must be a boolean")

    products = ProductReader().fetch_products(batch_size=500)
    products, brands_inferred = _inherit_registered_sibling_brands(products)
    with _SOURCE_PUBLISH_FENCE:
        with _store() as store:
            # Keep SQLite write-lock holds short enough that a concurrently
            # running product can durably seal a paid provider action without
            # timing out.
            results = []
            for offset in range(0, len(products), 100):
                results.extend(
                    store.upsert_products(products[offset : offset + 100])
                )
            quota_resumed = (
                store.resume_search_quota_waits()
                if resume_quota
                else 0
            )
            counts = store.status_counts()
    return {
        "ok": True,
        "source_records": len(products),
        "brands_inferred": brands_inferred,
        "created": sum(item.created for item in results),
        "changed": sum(item.changed and not item.created for item in results),
        "rescheduled": sum(item.rescheduled for item in results),
        "quota_resumed": quota_resumed,
        "queue": counts,
    }


def refresh_catalogue() -> dict[str, Any]:
    """Refresh source rows without waking still-exhausted Exa waits."""

    return sync_catalogue(resume_quota=False)


def _cmd_precheck(_args: argparse.Namespace) -> int:
    with _store() as store:
        due = store.list_due(limit=1_000_000)
        counts = store.status_counts()
    _emit(
        {
            "ok": True,
            "due": len(due),
            "workAvailable": bool(due),
            "counts": counts,
        }
    )
    return 0


def _cmd_claim(args: argparse.Namespace) -> int:
    with _store() as store:
        lease = store.lease_next(
            _worker_id(args.worker_id), lease_seconds=args.lease_seconds
        )
    if lease is None:
        _emit({"ok": True, "claimed": False})
        return 0
    _emit(
        {
            "ok": True,
            "claimed": True,
            "product": lease.payload,
            "product_id": lease.product_id,
            "source_hash": lease.source_hash,
            "lease_token": lease.token,
            "leased_until": lease.leased_until,
        }
    )
    return 0


def _run_search(
    store: StateStore,
    lease: Lease,
    *,
    max_results: int,
    timeout: float,
) -> dict[str, Any]:
    store.begin_search(lease)
    try:
        client = _make_search_client(timeout=timeout)
        bundle = client.search_product(
            _search_identity(lease.payload), max_results=max_results
        )
        store.finish_search(
            lease,
            [
                item["url"]
                for item in bundle.get("results", [])
                if isinstance(item, dict) and isinstance(item.get("url"), str)
            ],
            bundle.get("usage", {}),
        )
    except Exception as exc:
        try:
            store.record_outcome(
                lease,
                _provider_outcome(exc, locals().get("client")),
                error=_safe_error(exc),
            )
        except StateError:
            pass
        raise
    final_check = store.precheck(lease)
    if not final_check.ready:
        raise LeaseLostError(f"source changed during search: {final_check.reason}")
    return bundle


def _cmd_search(args: argparse.Namespace) -> int:
    with _store() as store:
        lease = _require_lease(store, args.lease_token)
        bundle = _run_search(
            store,
            lease,
            max_results=args.max_results,
            timeout=args.timeout,
        )
    _emit(
        {
            "ok": True,
            "product_id": lease.product_id,
            "lease_token": lease.token,
            "product": lease.payload,
            "search": bundle,
        }
    )
    return 0


def _bounded_pdf_target_models(
    product_id: Any,
    product_name: Any,
) -> tuple[str, ...]:
    """Fit catalogue hints to the direct PDF parser's public input contract."""

    candidates = catalogue_model_candidates(
        product_id,
        product_name,
        allow_product_id=True,
    )
    return tuple(
        candidate
        for candidate in candidates
        if len(candidate) <= _MAX_PDF_TARGET_MODEL_CHARS
    )[:_MAX_PDF_TARGET_MODELS]


def _prepare_extract_results(
    lease: Lease,
    bundle: dict[str, Any],
    *,
    submitted_urls: Sequence[str] | None = None,
) -> dict[str, int]:
    """Add direct-PDF evidence independently and finalize bounded extracts.

    Exa discovery/extraction and direct PDF retrieval are deliberately separate
    evidence paths.  A submitted PDF can therefore become a successful result
    even when Exa's ``/contents`` response reports no text for that URL.
    """

    settings = PDFSettings.from_env()
    limit = max_extract_chars()
    target_models = _bounded_pdf_target_models(
        lease.product_id,
        lease.payload.get("product_name"),
    )
    direct_summary = {
        "attempted": 0,
        "used": 0,
        "failed": 0,
        "identity_mismatch": 0,
    }
    raw_results = bundle.get("results")
    results = raw_results if isinstance(raw_results, list) else []
    if raw_results is not results:
        bundle["results"] = results

    # Provider payloads are untrusted. Only this runtime may attest that a
    # URL was downloaded and parsed as a PDF.
    for result in results:
        if not isinstance(result, dict):
            continue
        for field in _DIRECT_PDF_METADATA_FIELDS:
            result.pop(field, None)

    if settings.enabled:
        result_by_url = {
            result["url"]: result
            for result in results
            if (
                isinstance(result, dict)
                and isinstance(result.get("url"), str)
            )
        }
        candidates = (
            list(submitted_urls)
            if submitted_urls is not None
            else list(result_by_url)
        )
        for url in candidates:
            if direct_summary["attempted"] >= settings.max_files:
                break
            if not looks_like_pdf_url(url):
                continue
            result = result_by_url.get(url)
            synthetic = result is None
            if result is None:
                result = {"url": url}
            direct_summary["attempted"] += 1
            result["pdf_direct_status"] = "attempted"
            try:
                evidence = extract_pdf_evidence(
                    url,
                    max_bytes=settings.max_bytes,
                    max_pages=settings.max_pages,
                    max_chars=limit,
                    download_timeout=settings.download_timeout,
                    parse_timeout=settings.parse_timeout,
                    target_models=target_models,
                )
            except PDFDocumentError as exc:
                direct_summary["failed"] += 1
                if not synthetic:
                    result["pdf_direct_status"] = "failed"
                    result["pdf_direct_error_type"] = (
                        exc.__class__.__name__[:100]
                    )
                continue

            result["pdf_sha256"] = evidence.sha256
            result["pdf_page_count"] = evidence.page_count
            result["pdf_extracted_pages"] = evidence.extracted_pages
            result["pdf_final_url_changed"] = (
                evidence.final_url != evidence.requested_url
            )
            result["pdf_final_hostname"] = _url_hostname(
                evidence.final_url
            )
            result["pdf_parameter_rows"] = [
                {
                    "model": row.model,
                    "source_label": row.source_label,
                    "value": row.value,
                    "unit": row.unit,
                    "section": row.section,
                    "table_title": row.table_title,
                    "value_state": row.value_state,
                    "page": row.page,
                    "order": row.order,
                    "model_quote": row.model_quote,
                    "quote": row.quote,
                }
                for row in evidence.parameter_rows
            ]
            existing = result.get("raw_content")
            existing_text = existing if isinstance(existing, str) else ""
            direct_identity = text_contains_catalogue_identity(
                lease.product_id,
                lease.payload.get("product_name"),
                evidence.text,
            )
            existing_identity = (
                bool(existing_text.strip())
                and text_contains_catalogue_identity(
                    lease.product_id,
                    lease.payload.get("product_name"),
                    existing_text,
                )
            )
            direct_text = evidence.text
            result.pop("raw_content_sha256", None)
            if existing_text.strip():
                combined_text = (
                    direct_text.rstrip()
                    + "\n\n"
                    + existing_text.lstrip()
                )
                if len(combined_text) <= limit:
                    result["raw_content"] = combined_text
                    result["content_source"] = (
                        "direct_pdf_text+exa_text"
                    )
                    result["truncated"] = bool(
                        evidence.truncated or result.get("truncated")
                    )
                else:
                    if existing_identity and not direct_identity:
                        preferred_text = existing_text
                        preferred_source = str(
                            result.get("content_source") or "exa_full_text"
                        )
                    else:
                        preferred_text = direct_text
                        preferred_source = "direct_pdf_text"
                    if len(preferred_text) > limit:
                        result["raw_content_sha256"] = hashlib.sha256(
                            preferred_text.encode("utf-8")
                        ).hexdigest()
                    result["raw_content"] = preferred_text[:limit]
                    result["content_source"] = preferred_source
                    result["truncated"] = True
            else:
                result["raw_content"] = direct_text
                result["content_source"] = "direct_pdf_text"
                result["truncated"] = bool(evidence.truncated)

            if direct_identity:
                result["pdf_direct_status"] = "used"
                direct_summary["used"] += 1
            else:
                result["pdf_direct_status"] = "identity_mismatch"
                direct_summary["identity_mismatch"] += 1
            if synthetic:
                results.append(result)
                result_by_url[url] = result

    for result in results:
        if not isinstance(result, dict):
            continue
        content = result.get("raw_content")
        if isinstance(content, str) and len(content) > limit:
            result["raw_content_sha256"] = hashlib.sha256(
                content.encode("utf-8")
            ).hexdigest()
            result["raw_content"] = content[:limit]
            result["truncated"] = True
            content = result["raw_content"]
        result["identity_verified"] = (
            isinstance(content, str)
            and bool(content.strip())
            and text_contains_catalogue_identity(
                lease.product_id,
                lease.payload.get("product_name"),
                content,
            )
        )
    return direct_summary


def _run_extract(
    store: StateStore,
    lease: Lease,
    *,
    urls: list[str],
    query: str,
    timeout: float,
) -> dict[str, Any]:
    submitted = store.begin_extract(lease, urls)
    try:
        client = _make_search_client(timeout=timeout)
        bundle = client.extract_urls(submitted, query)
        _prepare_extract_results(
            lease,
            bundle,
            submitted_urls=submitted,
        )
        successful_urls = [
            item["url"]
            for item in bundle.get("results", [])
            if isinstance(item, dict)
            and isinstance(item.get("url"), str)
            and isinstance(item.get("raw_content"), str)
            and item["raw_content"].strip()
        ]
        store.finish_extract(
            lease,
            successful_urls,
            bundle.get("usage", {}),
        )
    except Exception as exc:
        try:
            store.record_outcome(
                lease,
                _provider_outcome(exc, locals().get("client")),
                error=_safe_error(exc),
            )
        except StateError:
            pass
        raise
    final_check = store.precheck(lease)
    if not final_check.ready:
        raise LeaseLostError(f"source changed during extract: {final_check.reason}")
    return bundle


def _cmd_extract(args: argparse.Namespace) -> int:
    request = _read_json(args.request_file)
    urls = request.get("urls")
    query = request.get("query")
    if not isinstance(urls, list) or not isinstance(query, str):
        raise CLIError("extract request needs string query and array urls")
    with _store() as store:
        lease = _require_lease(store, args.lease_token)
        bundle = _run_extract(
            store,
            lease,
            urls=urls,
            query=query,
            timeout=args.timeout,
        )
    _emit(
        {
            "ok": True,
            "product_id": lease.product_id,
            "lease_token": lease.token,
            "extract": bundle,
        }
    )
    return 0


def _reserve_research_action(
    store: StateStore,
    lease: Lease,
    *,
    round_number: int,
    action: str,
    request: Mapping[str, Any],
    scope_fingerprints: Mapping[str, str],
) -> str:
    try:
        action_scope = scope_fingerprints[action]
    except KeyError as exc:
        raise CLIError(f"missing research scope for {action}") from exc
    fingerprint = research_request_fingerprint(action, request)
    reservation = store.begin_research_action(
        lease,
        round_number=round_number,
        action=action,
        request_fingerprint=fingerprint,
        scope_fingerprint=action_scope,
        blocking_scope_fingerprints=scope_fingerprints,
    )
    if not reservation.should_execute:
        scope = (
            "this attempt"
            if reservation.record.attempt_id == lease.attempt_id
            else (
                "an earlier abnormal attempt for the same product/source "
                "revision"
            )
        )
        raise ResearchReplaySuppressedError(
            f"research {action} request is already {reservation.record.status} "
            f"in {scope}; the external request will not be replayed"
        )
    return fingerprint


def _research_scope_fingerprints(
    product: dict[str, Any],
    ai_settings: AISettings,
    search_client: ExaClient,
    *,
    max_results: int,
    research_settings: ResearchSettings,
) -> dict[str, str]:
    pdf_settings = PDFSettings.from_env()
    provider_credential = getattr(
        search_client,
        "credential_fingerprint",
        "",
    )
    if (
        not isinstance(provider_credential, str)
        or not re.fullmatch(r"[0-9a-f]{64}", provider_credential)
    ):
        provider_credential = "unavailable"
    provider_endpoint = getattr(
        search_client,
        "api_base_url",
        EXA_API_BASE_URL,
    )
    if not isinstance(provider_endpoint, str) or not provider_endpoint.startswith(
        "https://"
    ):
        provider_endpoint = EXA_API_BASE_URL
    search_contract = getattr(
        search_client,
        "search_contract_version",
        EXA_SEARCH_CONTRACT_VERSION,
    )
    if not isinstance(search_contract, str) or not search_contract:
        search_contract = EXA_SEARCH_CONTRACT_VERSION
    extract_contract = getattr(
        search_client,
        "extract_contract_version",
        EXA_EXTRACT_CONTRACT_VERSION,
    )
    if not isinstance(extract_contract, str) or not extract_contract:
        extract_contract = EXA_EXTRACT_CONTRACT_VERSION
    search_identity = _search_identity(product)

    def digest(payload: Mapping[str, Any]) -> str:
        return hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()

    search_provider_scope = {
        "provider": "exa",
        "endpoint": provider_endpoint,
        "search_identity": search_identity,
        "excluded_domains": sorted(supplier_search_excluded_domains()),
        "credential_fingerprint": provider_credential,
    }
    search_scope = digest(
        {
            **search_provider_scope,
            "version": search_contract,
            "action": "search",
            "max_results": max_results,
        }
    )
    extract_scope = digest(
        {
            **search_provider_scope,
            "version": extract_contract,
            "action": "extract",
            "max_extract_chars": max_extract_chars(),
            "direct_pdf": {
                "contract_version": PDF_EXTRACTION_CONTRACT_VERSION,
                "enabled": pdf_settings.enabled,
                "max_files": pdf_settings.max_files,
                "max_bytes": pdf_settings.max_bytes,
                "max_pages": pdf_settings.max_pages,
                "download_timeout": pdf_settings.download_timeout,
                "parse_timeout": pdf_settings.parse_timeout,
            },
        }
    )
    trusted_source_policy = _trusted_source_policy_for_product(product)
    ai_scope = digest(
        {
            "version": AI_RESEARCH_PROMPT_VERSION,
            "provider": "ai",
            # The public product prompt currently contains product_name; the
            # search identity additionally captures an enabled internal brand
            # hint because that can change the discovered manufacturer later
            # supplied to the AI request.
            "product_context": search_identity,
            "base_url": ai_settings.base_url,
            "model": ai_settings.model,
            "json_response_format": ai_settings.json_response_format,
            "thinking_mode": ai_settings.thinking_mode,
            "reasoning_effort": ai_settings.reasoning_effort,
            "trusted_source_policy": (
                {
                    "manufacturer": trusted_source_policy.manufacturer,
                    "domains": list(trusted_source_policy.domains),
                }
                if trusted_source_policy is not None
                else None
            ),
            "credential_fingerprint": hashlib.sha256(
                ai_settings.api_key.encode("utf-8")
            ).hexdigest(),
            "max_tokens": ai_settings.max_tokens,
            "max_evidence_chars": ai_settings.max_evidence_chars,
            "max_extract_chars": max_extract_chars(),
            "pdf_extraction_contract": PDF_EXTRACTION_CONTRACT_VERSION,
            "direct_pdf_enabled": pdf_settings.enabled,
            "retrieval_shape": {
                "max_results": max_results,
                "max_rounds": research_settings.max_rounds,
                "max_queries": research_settings.max_queries,
                "max_credits": research_settings.max_credits,
            },
        }
    )
    return {
        "search": search_scope,
        "extract": extract_scope,
        "ai": ai_scope,
    }


def _ai_provider_fingerprint(settings: AISettings) -> str:
    """Hash only the AI endpoint/account/model configuration, never its key."""

    return hashlib.sha256(
        json.dumps(
            {
                "base_url": settings.base_url,
                "model": settings.model,
                "credential_fingerprint": hashlib.sha256(
                    settings.api_key.encode("utf-8")
                ).hexdigest(),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _ai_output_fingerprint(settings: AISettings) -> str:
    """Scope invalid model output to the active prompt/action contract."""

    return hashlib.sha256(
        json.dumps(
            {
                "provider_fingerprint": _ai_provider_fingerprint(settings),
                "prompt_version": AI_RESEARCH_PROMPT_VERSION,
                "json_response_format": settings.json_response_format,
                "thinking_mode": settings.thinking_mode,
                "reasoning_effort": settings.reasoning_effort,
                "max_tokens": settings.max_tokens,
                "max_response_bytes": settings.max_response_bytes,
                "max_evidence_chars": settings.max_evidence_chars,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _exa_provider_fingerprint(client: Any) -> str:
    """Hash the active Exa endpoint and ordered credential set, never keys."""

    endpoint = getattr(client, "api_base_url", EXA_API_BASE_URL)
    if not isinstance(endpoint, str) or not endpoint.strip():
        endpoint = EXA_API_BASE_URL
    credential = getattr(client, "credential_fingerprint", "")
    if (
        not isinstance(credential, str)
        or re.fullmatch(r"[0-9a-f]{64}", credential) is None
    ):
        # The production client always exposes this digest. Keeping a stable
        # non-secret fallback makes injected offline clients auditable.
        credential = hashlib.sha256(b"unavailable").hexdigest()
    return hashlib.sha256(
        json.dumps(
            {
                "base_url": endpoint.strip(),
                "credential_fingerprint": credential,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _exa_failure_audit(
    client: Any,
    exc: BaseException,
) -> dict[str, Any]:
    """Return provider-global Exa failure metadata without request content."""

    audit: dict[str, Any] = {
        "provider_fingerprint": _exa_provider_fingerprint(client),
        "error_type": exc.__class__.__name__,
    }
    if isinstance(exc, ExaHTTPError) and isinstance(exc.status_code, int):
        audit["http_status"] = exc.status_code
    return audit


def _provider_request_count(
    client: Any,
    attribute: str,
    *,
    default: int = 0,
) -> int:
    value = getattr(client, attribute, 0)
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return min(100, max(0, value))


def _ai_response_audit(client: Any) -> dict[str, Any]:
    """Return bounded numeric provider metadata without response text."""

    metadata = getattr(client, "last_response_metadata", ())
    if not isinstance(metadata, (list, tuple)):
        return {}
    finish_reasons: list[str] = []
    usage_totals: dict[str, int] = {}

    def add_usage(value: Any) -> None:
        if not isinstance(value, Mapping):
            return
        for raw_key, raw_item in list(value.items())[:32]:
            key = str(raw_key)
            if isinstance(raw_item, Mapping):
                add_usage(raw_item)
                continue
            if (
                key not in _AI_TOKEN_USAGE_FIELDS
                or isinstance(raw_item, bool)
                or not isinstance(raw_item, (int, float))
                or not math.isfinite(float(raw_item))
                or float(raw_item) < 0
                or not float(raw_item).is_integer()
            ):
                continue
            total = usage_totals.get(key, 0) + int(raw_item)
            if total <= 1_000_000_000:
                usage_totals[key] = total

    for item in metadata[:2]:
        reason = getattr(item, "finish_reason", None)
        if (
            isinstance(reason, str)
            and re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", reason)
        ):
            finish_reasons.append(reason)
        add_usage(getattr(item, "usage", None))

    result: dict[str, Any] = {}
    if finish_reasons:
        result["finish_reasons"] = finish_reasons
    if usage_totals:
        result["usage_totals"] = usage_totals
    return result


def _known_provider_credits(client: Any) -> int | float:
    value = getattr(client, "last_operation_known_credits", 0)
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) < 0
    ):
        return 0
    number = float(value)
    return int(number) if number.is_integer() else number


def _search_failure_status(exc: BaseException, client: Any) -> str:
    del client
    if _provider_quota_error(exc):
        # A multi-query action may have completed earlier requests, but the
        # quota response definitively proves the current request did not run.
        # Known partial credits are audited and the action may retry after the
        # monthly wake instead of becoming permanently replay-suppressed.
        return "failed"
    if (
        isinstance(exc, ExaHTTPError)
        and isinstance(exc.status_code, int)
        and exc.status_code in EXA_DEFINITIVE_REJECT_HTTP_STATUSES
    ):
        return "failed"
    return "uncertain"


def _ai_failure_status(exc: BaseException, client: Any) -> str:
    del client
    if isinstance(exc, AILocalExecutionError):
        # Process isolation failed before a provider request could start.
        return "failed"
    if isinstance(exc, AIResponseError):
        # The provider returned a response that was oversized, malformed, or
        # still failed the bounded repair/contract. Delivery is known, so a
        # later bounded retry is safe; reserve ``uncertain`` for genuinely
        # unknown network outcomes.
        return "failed"
    if (
        isinstance(exc, AIHTTPError)
        and isinstance(exc.status_code, int)
        and exc.status_code in DEFINITIVE_REJECT_HTTP_STATUSES
    ):
        # A rejected repair can follow one known invalid response. Repeating
        # that bounded action is an audited retry, not an ambiguous replay.
        return "failed"
    return "uncertain"


def _record_research_service_failure(
    store: StateStore,
    lease: Lease,
    *,
    round_number: int,
    action: str,
    fingerprint: str,
    error: BaseException,
    outcome: str,
    result_summary: Mapping[str, Any] | None = None,
    status: str = "uncertain",
    known_credits: int | float | None = None,
) -> None:
    try:
        store.finish_research_action(
            lease,
            round_number=round_number,
            action=action,
            request_fingerprint=fingerprint,
            status=status,
            result_summary=result_summary,
            credits=(
                known_credits
                if known_credits is not None
                else (0 if status == "failed" else None)
            ),
            error=_safe_error(error),
        )
    except StateError:
        pass
    _record_active_failure(store, lease, outcome, error)


def _run_research_search(
    store: StateStore,
    lease: Lease,
    client: ExaClient,
    *,
    round_number: int,
    max_results: int,
    scope_fingerprints: Mapping[str, str],
    queries: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    identity = _search_identity(lease.payload)
    planned_queries = (
        list(queries)
        if queries is not None
        else build_queries(identity)
    )
    request = {
        "queries": planned_queries,
        "max_results": max_results,
        "search_depth": "basic",
    }
    fingerprint = _reserve_research_action(
        store,
        lease,
        round_number=round_number,
        action="search",
        request=request,
        scope_fingerprints=scope_fingerprints,
    )
    try:
        if queries is not None:
            search_kwargs: dict[str, Any] = {"max_results": max_results}
            excluded_domains = supplier_search_excluded_domains()
            if excluded_domains:
                search_kwargs["exclude_domains"] = sorted(excluded_domains)
            raw_bundle = client.search_queries(
                planned_queries,
                **search_kwargs,
            )
        else:
            raw_bundle = client.search_product(
                identity,
                max_results=max_results,
            )
        if not isinstance(raw_bundle, dict):
            raise ExaResponseError("Exa search result must be an object")
        bundle = dict(raw_bundle)
        executed_queries = bundle.get("queries")
        if (
            not isinstance(executed_queries, list)
            or not all(isinstance(item, str) for item in executed_queries)
        ):
            bundle["queries"] = planned_queries
        bundle.setdefault("planned_queries", planned_queries)
        candidate_urls = [
            item["url"]
            for item in bundle.get("results", [])
            if isinstance(item, dict) and isinstance(item.get("url"), str)
        ]
        request_ids = bundle.get("request_ids")
        store.finish_research_action(
            lease,
            round_number=round_number,
            action="search",
            request_fingerprint=fingerprint,
            status="completed",
            result_summary={
                "queries": bundle["queries"],
                "candidate_urls": candidate_urls,
                "request_ids": (
                    request_ids
                    if isinstance(request_ids, list)
                    else []
                ),
                "provider_requests": _provider_request_count(
                    client,
                    "last_operation_requests",
                    default=len(planned_queries),
                ),
            },
            credits=_credits(bundle),
        )
    except Exception as exc:
        if isinstance(exc, LeaseLostError):
            raise LeaseLostError(
                "source changed during search: lease_lost"
            ) from exc
        _record_research_service_failure(
            store,
            lease,
            round_number=round_number,
            action="search",
            fingerprint=fingerprint,
            error=exc,
            outcome=_provider_outcome(exc, client),
            status=_search_failure_status(exc, client),
            known_credits=(
                _known_provider_credits(client)
                if _known_provider_credits(client) > 0
                else None
            ),
            result_summary={
                "queries": planned_queries,
                "provider_requests": _provider_request_count(
                    client,
                    "last_operation_requests",
                    default=len(planned_queries),
                ),
                "completed_provider_requests": _provider_request_count(
                    client,
                    "last_operation_completed_requests",
                ),
                "known_partial_credits": _known_provider_credits(client),
                **_exa_failure_audit(client, exc),
            },
        )
        raise
    check = store.precheck(lease)
    if not check.ready:
        raise LeaseLostError(f"source changed during search: {check.reason}")
    return bundle


def _run_research_extract(
    store: StateStore,
    lease: Lease,
    client: ExaClient,
    *,
    round_number: int,
    scope_fingerprints: Mapping[str, str],
    urls: list[str],
    query: str,
) -> dict[str, Any]:
    request = {"urls": urls, "query": query}
    fingerprint = _reserve_research_action(
        store,
        lease,
        round_number=round_number,
        action="extract",
        request=request,
        scope_fingerprints=scope_fingerprints,
    )
    try:
        raw_bundle = client.extract_urls(urls, query)
        if not isinstance(raw_bundle, dict):
            raise ExaResponseError("Exa extract result must be an object")
        bundle = dict(raw_bundle)
        successful_urls: list[str] = []
        successful_url_set: set[str] = set()
        results = bundle.get("results")
        if isinstance(results, list):
            submitted_url_set = set(urls)
            if any(
                isinstance(result, Mapping)
                and isinstance(result.get("url"), str)
                and result["url"] not in submitted_url_set
                for result in results
            ):
                raise ExaResponseError(
                    "Exa extract returned a URL outside this action's "
                    "submitted set"
                )
        direct_pdf_summary = _prepare_extract_results(
            lease,
            bundle,
            submitted_urls=urls,
        )
        for result in bundle["results"]:
            if not isinstance(result, dict):
                continue
            content = result.get("raw_content")
            if (
                isinstance(content, str)
                and bool(content.strip())
                and isinstance(result.get("url"), str)
                and result["url"] not in successful_url_set
            ):
                successful_url_set.add(result["url"])
                successful_urls.append(result["url"])
        store.finish_research_action(
            lease,
            round_number=round_number,
            action="extract",
            request_fingerprint=fingerprint,
            status="completed",
            result_summary={
                "submitted_urls": urls,
                "successful_urls": successful_urls,
                "provider_requests": _provider_request_count(
                    client,
                    "last_operation_requests",
                    default=1,
                ),
                "direct_pdf": direct_pdf_summary,
            },
            credits=_credits(bundle),
        )
    except Exception as exc:
        if isinstance(exc, LeaseLostError):
            raise LeaseLostError(
                "source changed during extract: lease_lost"
            ) from exc
        _record_research_service_failure(
            store,
            lease,
            round_number=round_number,
            action="extract",
            fingerprint=fingerprint,
            error=exc,
            outcome=_provider_outcome(exc, client),
            status=_search_failure_status(exc, client),
            known_credits=(
                _known_provider_credits(client)
                if _known_provider_credits(client) > 0
                else None
            ),
            result_summary={
                "submitted_urls": urls,
                "provider_requests": _provider_request_count(
                    client,
                    "last_operation_requests",
                    default=1,
                ),
                "completed_provider_requests": _provider_request_count(
                    client,
                    "last_operation_completed_requests",
                ),
                "known_partial_credits": _known_provider_credits(client),
                **_exa_failure_audit(client, exc),
            },
        )
        raise
    check = store.precheck(lease)
    if not check.ready:
        raise LeaseLostError(f"source changed during extract: {check.reason}")
    return bundle


def _research_context_digest(
    search: Mapping[str, Any],
    extract: Mapping[str, Any],
) -> str:
    encoded = json.dumps(
        {"search": search, "extract": extract},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _run_research_ai(
    store: StateStore,
    lease: Lease,
    client: OpenAICompatibleClient,
    settings: AISettings,
    *,
    round_number: int,
    scope_fingerprints: Mapping[str, str],
    search: Mapping[str, Any],
    extract: Mapping[str, Any],
    candidate_manufacturer: str | None,
    previous_queries: tuple[str, ...],
    validation_feedback: ValidationFeedback | None,
    trusted_source_policy: TrustedSourcePolicy | None,
    final_only: bool,
) -> tuple[FinalAction | SearchMoreAction, int]:
    request = {
        "model": settings.model,
        "context_sha256": _research_context_digest(search, extract),
        "candidate_manufacturer": candidate_manufacturer or "",
        "previous_queries": list(previous_queries),
        "validation_feedback_gap": (
            validation_feedback.gap.value
            if validation_feedback is not None
            else ""
        ),
        "final_only": final_only,
        "trusted_source_policy": (
            {
                "manufacturer": trusted_source_policy.manufacturer,
                "domains": list(trusted_source_policy.domains),
            }
            if trusted_source_policy is not None
            else None
        ),
    }
    fingerprint = _reserve_research_action(
        store,
        lease,
        round_number=round_number,
        action="ai",
        request=request,
        scope_fingerprints=scope_fingerprints,
    )
    try:
        action = client.next_research_action(
            product=_search_identity(lease.payload),
            search=search,
            extract=extract,
            candidate_manufacturer=candidate_manufacturer,
            previous_queries=previous_queries,
            validation_feedback=validation_feedback,
            trusted_source_policy=trusted_source_policy,
            final_only=final_only,
        )
        provider_requests = _provider_request_count(
            client,
            "last_research_provider_requests",
            default=1,
        )
        summary: dict[str, Any]
        if isinstance(action, SearchMoreAction):
            summary = {
                "action_type": "search_more",
                "gap": action.gap.value,
                "queries": list(action.queries),
            }
        elif isinstance(action, FinalAction):
            summary = {
                "action_type": "final",
                "outcome": str(action.decision.get("outcome") or "")[:100],
                "manufacturer": str(
                    action.decision.get("manufacturer") or ""
                )[:300],
            }
        else:
            raise AIError("AI research action has an unexpected local type")
        summary["provider_requests"] = provider_requests
        summary.update(_ai_response_audit(client))
        store.finish_research_action(
            lease,
            round_number=round_number,
            action="ai",
            request_fingerprint=fingerprint,
            status="completed",
            result_summary=summary,
            credits=0,
        )
    except Exception as exc:
        if isinstance(exc, LeaseLostError):
            raise LeaseLostError(
                "source changed during AI analysis: lease_lost"
            ) from exc
        failure_summary: dict[str, Any] = {
            "provider_requests": _provider_request_count(
                client,
                "last_research_provider_requests",
                default=1,
            ),
            "provider_fingerprint": (
                _ai_output_fingerprint(settings)
                if isinstance(exc, AIResponseError)
                else _ai_provider_fingerprint(settings)
            ),
            "error_type": exc.__class__.__name__,
        }
        if isinstance(exc, AIInvalidOutputError):
            failure_summary["error_category"] = exc.category.value
        elif isinstance(exc, AIResponseError):
            failure_summary["error_category"] = (
                AIOutputErrorCategory.PROVIDER_ENVELOPE.value
            )
        failure_summary.update(_ai_response_audit(client))
        if isinstance(exc, AIHTTPError) and isinstance(
            exc.status_code,
            int,
        ):
            failure_summary["http_status"] = exc.status_code
        _record_research_service_failure(
            store,
            lease,
            round_number=round_number,
            action="ai",
            fingerprint=fingerprint,
            error=exc,
            outcome="ai_error" if isinstance(exc, AIError) else "error",
            result_summary=failure_summary,
            status=_ai_failure_status(exc, client),
        )
        raise
    check = store.precheck(lease)
    if not check.ready:
        raise LeaseLostError(
            f"source changed during AI analysis: {check.reason}"
        )
    return action, provider_requests


def _validate_decision_for_lease(
    store: StateStore,
    lease: Lease,
    raw: dict[str, Any],
    *,
    evidence_text_by_url: dict[str, str] | None = None,
    allowed_classification_urls: set[str] | None = None,
    classification_text_by_url: dict[str, str] | None = None,
    verified_primary_document_urls: set[str] | None = None,
    fact_diagnostics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate a proposal without mutating the attempt's terminal outcome."""

    allowed_urls = set(store.allowed_evidence_urls(lease.token))
    operator_manufacturer = public_brand_alias(
        str(lease.payload.get("brand_code") or "")
    )
    trusted_domains = (
        trusted_source_domains_for_product(
            str(lease.payload.get("brand_code") or ""),
            raw.get("manufacturer")
            if isinstance(raw.get("manufacturer"), str)
            else None,
        )
        if raw.get("outcome") == "publish"
        else frozenset()
    )
    if (
        raw.get("outcome") == "publish"
        and trusted_domains
        and not operator_manufacturer
    ):
        raise SourceVerificationError(
            "a trusted catalogue-brand domain override requires an explicit "
            "public manufacturer alias"
        )

    def validate(candidate: Mapping[str, Any]) -> dict[str, Any]:
        return validate_decision(
            candidate,
            expected_product_id=lease.product_id,
            expected_lease_token=lease.token,
            minimum_confidence=min_publish_confidence(),
            minimum_fact_confidence=min_fact_confidence(),
            mirrors_allowed=allow_mirrors(),
            allowed_evidence_urls=allowed_urls,
            allowed_classification_urls=(
                allowed_classification_urls
                if allowed_classification_urls is not None
                else allowed_urls
            ),
            trusted_source_domains=trusted_domains,
            operator_manufacturer_identity=operator_manufacturer or None,
            expected_product_name=str(
                lease.payload.get("product_name") or ""
            ),
            evidence_text_by_url=evidence_text_by_url,
            classification_text_by_url=(
                classification_text_by_url
                if classification_text_by_url is not None
                else evidence_text_by_url
            ),
            verified_primary_document_urls=verified_primary_document_urls,
        )

    if fact_diagnostics is not None:
        fact_diagnostics.clear()
    if raw.get("outcome") != "publish":
        return validate(raw)

    raw_facts = raw.get("facts")
    if not isinstance(raw_facts, list):
        return validate(raw)
    raw_parameters = raw.get("datasheet_parameters", [])
    raw_insights = raw.get("derived_insights", [])
    if not isinstance(raw_parameters, list) or not isinstance(raw_insights, list):
        return validate(raw)

    # Facts, source-table rows, and AI-derived insights are optional enrichment.
    # Validate the publication authority and reader metadata first, then admit
    # each enrichment item independently so one malformed row cannot poison an
    # otherwise verified product. Duplicate normalized-name groups are dropped
    # as a whole rather than resolving a conflict by choosing an arbitrary item.
    fact_name_counts: dict[str, int] = {}
    fact_name_keys: list[str | None] = []
    for item in raw_facts:
        name = item.get("name") if isinstance(item, Mapping) else None
        key = identity_key(name) if isinstance(name, str) else ""
        normalized_key = key or None
        fact_name_keys.append(normalized_key)
        if normalized_key is not None:
            fact_name_counts[normalized_key] = (
                fact_name_counts.get(normalized_key, 0) + 1
            )
    unique_facts = [
        item
        for item, key in zip(raw_facts, fact_name_keys, strict=True)
        if key is not None and fact_name_counts[key] == 1
    ]
    invalid_name_count = sum(key is None for key in fact_name_keys)
    duplicate_name_count = sum(
        key is not None and fact_name_counts[key] > 1
        for key in fact_name_keys
    )
    limited_facts = unique_facts[:100]
    limit_exceeded_count = len(unique_facts) - len(limited_facts)

    base = {
        **raw,
        "facts": [],
        "datasheet_parameters": [],
        "derived_insights": [],
    }
    validated = validate(base)
    retained_facts: list[Any] = []
    validation_failed_count = 0
    for fact in limited_facts:
        candidate = {
            **base,
            "facts": [*retained_facts, fact],
        }
        try:
            candidate_validated = validate(candidate)
        except DecisionError:
            validation_failed_count += 1
            continue
        retained_facts.append(fact)
        validated = candidate_validated

    parameter_counts: dict[tuple[str, str], int] = {}
    parameter_keys: list[tuple[str, str] | None] = []
    for item in raw_parameters:
        if not isinstance(item, Mapping):
            parameter_keys.append(None)
            continue
        name = item.get("name")
        section = item.get("section", "")
        if not isinstance(name, str) or not isinstance(section, str):
            parameter_keys.append(None)
            continue
        key = (identity_key(section), identity_key(name))
        normalized_key = key if key[1] else None
        parameter_keys.append(normalized_key)
        if normalized_key is not None:
            parameter_counts[normalized_key] = (
                parameter_counts.get(normalized_key, 0) + 1
            )
    unique_parameters = [
        item
        for item, key in zip(raw_parameters, parameter_keys, strict=True)
        if key is not None and parameter_counts[key] == 1
    ][:_MAX_LOCAL_DATASHEET_PARAMETERS]
    retained_parameters: list[Any] = []
    for parameter in unique_parameters:
        candidate = {
            **base,
            "facts": retained_facts,
            "datasheet_parameters": [*retained_parameters, parameter],
        }
        try:
            candidate_validated = validate(candidate)
        except DecisionError:
            continue
        retained_parameters.append(parameter)
        validated = candidate_validated

    insight_counts: dict[str, int] = {}
    insight_keys: list[str | None] = []
    for item in raw_insights:
        name = item.get("name") if isinstance(item, Mapping) else None
        key = identity_key(name) if isinstance(name, str) else ""
        normalized_key = key or None
        insight_keys.append(normalized_key)
        if normalized_key is not None:
            insight_counts[normalized_key] = insight_counts.get(normalized_key, 0) + 1
    unique_insights = [
        item
        for item, key in zip(raw_insights, insight_keys, strict=True)
        if key is not None and insight_counts[key] == 1
    ][:20]
    retained_insights: list[Any] = []
    for insight in unique_insights:
        candidate = {
            **base,
            "facts": retained_facts,
            "datasheet_parameters": retained_parameters,
            "derived_insights": [*retained_insights, insight],
        }
        try:
            candidate_validated = validate(candidate)
        except DecisionError:
            continue
        retained_insights.append(insight)
        validated = candidate_validated

    if fact_diagnostics is not None:
        rejection_reasons = {
            reason: count
            for reason, count in {
                "duplicate_name": duplicate_name_count,
                "invalid_name": invalid_name_count,
                "limit_exceeded": limit_exceeded_count,
                "validation_failed": validation_failed_count,
            }.items()
            if count
        }
        rejected = len(raw_facts) - len(retained_facts)
        fact_diagnostics.update(
            {
                "complete": True,
                "proposed": len(raw_facts),
                "retained": len(retained_facts),
                "rejected": rejected,
                "rejection_reasons": rejection_reasons,
            }
        )
    return validated


def _apply_decision(
    store: StateStore,
    lease: Lease,
    raw: dict[str, Any],
    *,
    audit: dict[str, Any] | None = None,
    evidence_text_by_url: dict[str, str] | None = None,
    allowed_classification_urls: set[str] | None = None,
    classification_text_by_url: dict[str, str] | None = None,
    verified_primary_document_urls: set[str] | None = None,
    validated_decision: dict[str, Any] | None = None,
    fact_diagnostics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    diagnostics = fact_diagnostics if fact_diagnostics is not None else {}
    try:
        decision = (
            validated_decision
            if validated_decision is not None
            else _validate_decision_for_lease(
                store,
                lease,
                raw,
                evidence_text_by_url=evidence_text_by_url,
                allowed_classification_urls=allowed_classification_urls,
                classification_text_by_url=classification_text_by_url,
                verified_primary_document_urls=(
                    verified_primary_document_urls
                ),
                fact_diagnostics=diagnostics,
            )
        )
    except SourceVerificationError as exc:
        outcome = store.record_outcome(
            lease,
            "source_unverified",
            error=_safe_error(exc),
            details={
                "discovered_manufacturer": str(
                    raw.get("manufacturer") or ""
                )[:300],
                "automation": audit or {},
            },
        )
        return {
            "ok": True,
            "processed": True,
            "published": False,
            "product_id": lease.product_id,
            "outcome": outcome.outcome,
            "next_attempt_at": outcome.next_attempt_at,
        }
    except DecisionError as exc:
        outcome = store.record_outcome(
            lease,
            "invalid_decision",
            error=_safe_error(exc),
        )
        return {
            "ok": True,
            "processed": True,
            "published": False,
            "product_id": lease.product_id,
            "outcome": outcome.outcome,
            "next_attempt_at": outcome.next_attempt_at,
        }
    except ConfigError as exc:
        store.record_outcome(
            lease,
            "configuration_error",
            error=_safe_error(exc),
        )
        raise

    audit_payload: dict[str, Any] = {
        "decision": decision,
        "content_schema_version": INITIAL_CONTENT_SCHEMA_VERSION,
        "validation_policy_fingerprint": _validation_policy_fingerprint(),
    }
    if decision["outcome"] == "publish":
        if not diagnostics:
            retained = len(decision.get("facts", []))
            diagnostics.update(
                {
                    "complete": False,
                    "proposed": retained,
                    "retained": retained,
                    "rejected": 0,
                    "rejection_reasons": {},
                }
            )
        audit_payload["fact_diagnostics"] = diagnostics
    if audit:
        audit_payload["automation"] = audit

    if decision["outcome"] != "publish":
        outcome = store.record_outcome(
            lease,
            decision["outcome"],
            payload=audit_payload,
        )
        return {
            "ok": True,
            "processed": True,
            "published": False,
            "product_id": lease.product_id,
            "outcome": outcome.outcome,
            "next_attempt_at": outcome.next_attempt_at,
        }

    path: str | None = None
    try:
        settings = WikiSettings.from_env()
        path = stable_path(lease.payload, prefix=settings.path_prefix)
        fields = _verified_wiki_page_fields(
            lease.product_id,
            lease.payload,
            decision,
            checked_at=datetime.now(timezone.utc),
        )

        client = WikiJSClient(
            settings.base_url,
            settings.token,
            timeout=settings.timeout,
            new_page_private=settings.new_page_private,
            new_page_published=settings.new_page_published,
        )
    except Exception as exc:
        failure = (
            "wikijs_conflict"
            if isinstance(exc, WikiJSConflictError)
            else "wikijs_error"
            if isinstance(exc, WikiJSError)
            else "publish_error"
        )
        try:
            outcome = store.record_outcome(
                lease,
                failure,
                payload=audit_payload,
                wiki_path=path,
                error=_safe_error(exc),
            )
        except StateError:
            outcome = None
        if isinstance(exc, WikiJSConflictError) and outcome is not None:
            return {
                "ok": True,
                "processed": True,
                "published": False,
                "product_id": lease.product_id,
                "outcome": outcome.outcome,
                "next_attempt_at": outcome.next_attempt_at,
            }
        raise

    # Catalogue refresh may overlap long paid research, but it must not update
    # the leased source between this final check and the externally visible
    # Wiki mutation. Keep the outcome write inside the same fence so a source
    # change is applied only before publication or after it is fully audited.
    with _SOURCE_PUBLISH_FENCE, store.publication_fence():
        try:
            check = store.precheck(lease)
            if not check.ready:
                raise LeaseLostError(
                    f"lease cannot publish: {check.reason}"
                )
            result = client.upsert_page(
                path,
                settings.locale,
                fields["title"],
                fields["description"],
                fields["managed_content"],
                fields["tags"],
            )
        except LeaseLostError:
            raise
        except Exception as exc:
            failure = (
                "wikijs_conflict"
                if isinstance(exc, WikiJSConflictError)
                else "wikijs_error"
                if isinstance(exc, WikiJSError)
                else "publish_error"
            )
            try:
                failure_outcome = store.record_outcome(
                    lease,
                    failure,
                    payload=audit_payload,
                    wiki_path=path,
                    error=_safe_error(exc),
                )
            except StateError:
                failure_outcome = None
            if (
                isinstance(exc, WikiJSConflictError)
                and failure_outcome is not None
            ):
                return {
                    "ok": True,
                    "processed": True,
                    "published": False,
                    "product_id": lease.product_id,
                    "outcome": failure_outcome.outcome,
                    "next_attempt_at": failure_outcome.next_attempt_at,
                }
            raise

        outcome = store.record_outcome(
            lease,
            "synced",
            payload={
                **audit_payload,
                "wiki_action": result.get("action"),
            },
            wiki_path=path,
            content_schema_version=INITIAL_CONTENT_SCHEMA_VERSION,
        )
    page = result.get("page") if isinstance(result.get("page"), dict) else {}
    return {
        "ok": True,
        "processed": True,
        "published": True,
        "product_id": lease.product_id,
        "outcome": outcome.outcome,
        "wiki": {
            "action": result.get("action"),
            "id": page.get("id"),
            "path": path,
            "locale": settings.locale,
        },
        "sources": len(decision["datasheets"]) + len(decision["sources"]),
        "next_attempt_at": outcome.next_attempt_at,
    }


def _cmd_publish(args: argparse.Namespace) -> int:
    raw = _read_json(args.decision_file)
    evidence_text_by_url = (
        _read_evidence_texts(args.evidence_file)
        if args.evidence_file
        else None
    )
    with _store() as store:
        lease = _require_lease(store, str(raw.get("lease_token") or ""))
        result = _apply_decision(
            store,
            lease,
            raw,
            evidence_text_by_url=evidence_text_by_url,
            classification_text_by_url=evidence_text_by_url,
        )
    _emit(result)
    return 0


def _nonpublish_decision(
    lease: Lease,
    outcome: str,
    note: str,
) -> dict[str, Any]:
    return {
        "schema_version": "1",
        "product_id": lease.product_id,
        "lease_token": lease.token,
        "outcome": outcome,
        "confidence": 1.0,
        "manufacturer": "",
        "manufacturer_zh": "",
        "model": preferred_catalogue_model(
            lease.product_id,
            lease.payload.get("product_name"),
        ),
        "display_title_zh": "",
        "product_category": "",
        "summary": "",
        "review_summary": "",
        "review_evidence_urls": [],
        "classification_evidence_urls": [],
        "classification_evidence_quotes": [],
        "decision_notes": note,
        "datasheets": [],
        "sources": [],
        "facts": [],
        "conflicts": [],
    }


def _record_active_failure(
    store: StateStore,
    lease: Lease,
    outcome: str,
    error: BaseException | str,
) -> None:
    try:
        active = store.get_by_lease(lease.token)
        if active is not None:
            safe_exception = (
                error if isinstance(error, BaseException) else Exception(error)
            )
            store.record_outcome(
                active,
                outcome,
                error=_safe_error(safe_exception),
            )
    except StateError:
        pass


def _recover_validated_publish(
    store: StateStore,
    lease: Lease,
) -> tuple[dict[str, Any], int] | None:
    """Recover a same-source validated decision after a Wiki-only failure."""

    history = store.attempt_history(lease.product_id)
    for attempt in reversed(history):
        if attempt.attempt_id == lease.attempt_id:
            continue
        if attempt.source_hash != lease.source_hash:
            return None
        if attempt.outcome not in {
            "wikijs_conflict",
            "wikijs_error",
            "publish_error",
        }:
            return None
        details = attempt.details
        recorded_payload = (
            details.get("payload")
            if isinstance(details, Mapping)
            else None
        )
        decision = (
            recorded_payload.get("decision")
            if isinstance(recorded_payload, Mapping)
            else None
        )
        if (
            not isinstance(decision, Mapping)
            or decision.get("outcome") != "publish"
            or recorded_payload.get("validation_policy_fingerprint")
            != _validation_policy_fingerprint()
        ):
            return None
        recovered = dict(decision)
        recovered["schema_version"] = "1"
        recovered["product_id"] = lease.product_id
        recovered["lease_token"] = lease.token
        return recovered, attempt.attempt_id
    return None


def _validation_policy_fingerprint() -> str:
    """Hash every operator-controlled source/decision policy input."""

    domain_map = {
        identity: sorted(domains)
        for identity, domains in sorted(
            trusted_source_domain_map().items()
        )
    }
    payload = {
        "version": VALIDATION_POLICY_VERSION,
        "minimum_publish_confidence": min_publish_confidence(),
        "minimum_fact_confidence": min_fact_confidence(),
        "mirrors_allowed": allow_mirrors(),
        "trusted_source_domains": domain_map,
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _research_extract_query(product_name: str, gap: str | None) -> str:
    focus = {
        "manufacturer_identity": "manufacturer identity product type",
        "primary_datasheet": "official datasheet complete specifications",
        "independent_corroboration": "independent specifications corroboration",
        "missing_exact_fact": (
            "complete specifications input output efficiency protection "
            "communication dimensions weight"
        ),
        "conflict_resolution": "exact model revision specifications",
        "scope_classification": "manufacturer product type category",
    }.get(
        gap or "",
        (
            "official datasheet complete specifications input output efficiency "
            "protection communication dimensions weight"
        ),
    )
    return f"{product_name[:180]} {focus}"[:400]


def _research_evidence_context(
    store: StateStore,
    lease: Lease,
    extract: Mapping[str, Any],
) -> tuple[
    dict[str, str],
    set[str],
    dict[str, str],
    set[str],
]:
    successful_extract_urls = set(store.allowed_evidence_urls(lease.token))
    trusted_domains = trusted_source_domains_for_product(
        str(lease.payload.get("brand_code") or ""),
        None,
    )
    evidence_text_by_url: dict[str, str] = {}
    classification_text_by_url: dict[str, str] = {}
    verified_primary_document_urls: set[str] = set()
    results = extract.get("results")
    if isinstance(results, list):
        for item in results:
            if not isinstance(item, Mapping):
                continue
            url = item.get("url")
            content = item.get("raw_content")
            if (
                not isinstance(url, str)
                or not isinstance(content, str)
                or not content.strip()
            ):
                continue
            classification_text_by_url[url] = content
            if url in successful_extract_urls:
                evidence_text_by_url[url] = content
            sha256 = item.get("pdf_sha256")
            page_count = item.get("pdf_page_count")
            extracted_pages = item.get("pdf_extracted_pages")
            if (
                url in successful_extract_urls
                and looks_like_pdf_url(url)
                and item.get("pdf_direct_status")
                in _DIRECT_PDF_SUCCESS_STATUSES
                and isinstance(sha256, str)
                and re.fullmatch(r"[0-9a-fA-F]{64}", sha256) is not None
                and isinstance(page_count, int)
                and not isinstance(page_count, bool)
                and page_count > 0
                and isinstance(extracted_pages, int)
                and not isinstance(extracted_pages, bool)
                and 0 < extracted_pages <= page_count
                and _pdf_redirect_allows_publication(
                    url,
                    item.get("pdf_final_hostname"),
                    trusted_domains=trusted_domains,
                )
            ):
                verified_primary_document_urls.add(url)
    return (
        evidence_text_by_url,
        set(classification_text_by_url),
        classification_text_by_url,
        verified_primary_document_urls,
    )


def _with_locally_bound_datasheet_parameters(
    raw: Mapping[str, Any],
    extract: Mapping[str, Any],
) -> dict[str, Any]:
    """Replace model-authored table rows with locally bound primary-PDF rows.

    The direct PDF runtime strips provider-supplied attestation fields before it
    creates ``pdf_parameter_rows``.  This final binding also requires the AI's
    selected primary URL, exact selected model, and both auditable TSV rows to
    survive in the bounded evidence body used by the decision validator.
    """

    decision = dict(raw)
    if decision.get("outcome") != "publish":
        return decision
    selected_model = decision.get("model")
    selected_model_key = (
        identity_key(selected_model)
        if isinstance(selected_model, str)
        else ""
    )
    datasheets = decision.get("datasheets")
    if not selected_model_key or not isinstance(datasheets, list):
        return decision
    primary_urls = {
        item.get("url")
        for item in datasheets
        if (
            isinstance(item, Mapping)
            and item.get("is_primary") is True
            and isinstance(item.get("url"), str)
        )
    }
    if not primary_urls:
        return decision

    results = extract.get("results")
    if not isinstance(results, list):
        return decision
    parameters: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for result in results:
        if (
            not isinstance(result, Mapping)
            or result.get("url") not in primary_urls
            or result.get("pdf_direct_status") != "used"
        ):
            continue
        body = result.get("raw_content")
        rows = result.get("pdf_parameter_rows")
        if not isinstance(body, str) or not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            model = row.get("model")
            name = row.get("source_label")
            value = row.get("value")
            unit = row.get("unit")
            source_section = row.get("section")
            table_title = row.get("table_title", "")
            value_state = row.get("value_state", "explicit")
            model_quote = row.get("model_quote")
            quote = row.get("quote")
            if (
                not isinstance(model, str)
                or identity_key(model) != selected_model_key
                or not isinstance(name, str)
                or not name.strip()
                or len(name) > 200
                or not isinstance(value, str)
                or not value.strip()
                or len(value) > 500
                or not isinstance(unit, str)
                or len(unit) > 80
                or not isinstance(source_section, str)
                or len(source_section) > 100
                or not isinstance(table_title, str)
                or len(table_title) > 200
                or value_state not in {"explicit", "merged_shared"}
                or not isinstance(model_quote, str)
                or not model_quote.strip()
                or len(model_quote) > 500
                or model_quote not in body
                or not isinstance(quote, str)
                or not quote.strip()
                or len(quote) > 500
                or quote not in body
            ):
                continue
            normalized_title = table_title.strip()
            normalized_source_section = source_section.strip()
            section = (
                normalized_title
                if normalized_title and len(normalized_title) <= 100
                else normalized_source_section
            )
            subsection = (
                normalized_source_section
                if normalized_title
                and len(normalized_title) <= 100
                and identity_key(normalized_title)
                != identity_key(normalized_source_section)
                else ""
            )
            key = (
                identity_key(section),
                identity_key(subsection),
                identity_key(name),
            )
            if not key[2] or key in seen:
                continue
            seen.add(key)
            parameters.append(
                {
                    "name": name.strip(),
                    "section": section,
                    "subsection": subsection,
                    "value": value.strip(),
                    "unit": unit.strip(),
                    "confidence": 1.0,
                    "evidence_urls": [result["url"]],
                    "evidence_quotes": [
                        {
                            "url": result["url"],
                            "model_quote": model_quote,
                            "quote": quote,
                        }
                    ],
                }
            )
            if len(parameters) >= _MAX_LOCAL_DATASHEET_PARAMETERS:
                break
        if len(parameters) >= _MAX_LOCAL_DATASHEET_PARAMETERS:
            break
    if parameters:
        decision["datasheet_parameters"] = parameters
    return decision


def _refresh_decision_parameters_from_primary_pdf(
    product_id: str,
    payload: Mapping[str, Any],
    decision: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Re-read one previously verified primary PDF for a content-only refresh."""

    settings = PDFSettings.from_env()
    if not settings.enabled:
        raise CLIError(
            "parameter hydration requires PV_WIKI_PDF_DIRECT_FETCH=true"
        )
    datasheets = decision.get("datasheets")
    if not isinstance(datasheets, list):
        raise CLIError("synced decision has no datasheet list")
    primary_items = [
        item
        for item in datasheets
        if (
            isinstance(item, Mapping)
            and item.get("is_primary") is True
            and item.get("source_type") == "manufacturer"
            and isinstance(item.get("url"), str)
        )
    ]
    if len(primary_items) != 1:
        raise CLIError(
            "parameter hydration requires exactly one primary manufacturer PDF"
        )
    url = str(primary_items[0]["url"])
    if not looks_like_pdf_url(url):
        raise CLIError("primary manufacturer document is not a PDF URL")

    targets = list(
        _bounded_pdf_target_models(
            product_id,
            payload.get("product_name"),
        )
    )
    selected_model = decision.get("model")
    if (
        isinstance(selected_model, str)
        and selected_model.strip()
        and len(selected_model.strip()) <= _MAX_PDF_TARGET_MODEL_CHARS
        and all(
            identity_key(selected_model) != identity_key(target)
            for target in targets
        )
    ):
        targets.insert(0, selected_model.strip())
    target_models = tuple(targets[:_MAX_PDF_TARGET_MODELS])
    evidence = extract_pdf_evidence(
        url,
        max_bytes=settings.max_bytes,
        max_pages=settings.max_pages,
        max_chars=max_extract_chars(),
        download_timeout=settings.download_timeout,
        parse_timeout=settings.parse_timeout,
        target_models=target_models,
    )
    if not text_contains_catalogue_identity(
        product_id,
        payload.get("product_name"),
        evidence.text,
    ):
        raise SourceVerificationError(
            "primary PDF no longer contains the catalogue identity"
        )
    trusted_domains = trusted_source_domains_for_product(
        str(payload.get("brand_code") or ""),
        decision.get("manufacturer")
        if isinstance(decision.get("manufacturer"), str)
        else None,
    )
    final_hostname = _url_hostname(evidence.final_url)
    if not _pdf_redirect_allows_publication(
        url,
        final_hostname,
        trusted_domains=trusted_domains,
    ):
        raise SourceVerificationError(
            "primary PDF redirect is outside the verified publication domain"
        )

    row_payloads = [
        {
            "model": row.model,
            "source_label": row.source_label,
            "value": row.value,
            "unit": row.unit,
            "section": row.section,
            "table_title": row.table_title,
            "value_state": row.value_state,
            "page": row.page,
            "order": row.order,
            "model_quote": row.model_quote,
            "quote": row.quote,
        }
        for row in evidence.parameter_rows
    ]
    bound = _with_locally_bound_datasheet_parameters(
        decision,
        {
            "results": [
                {
                    "url": url,
                    "raw_content": evidence.text,
                    "pdf_direct_status": "used",
                    "pdf_parameter_rows": row_payloads,
                }
            ]
        },
    )
    bound_parameters = bound.get("datasheet_parameters")
    if not isinstance(bound_parameters, list) or not bound_parameters:
        raise CLIError(
            "primary PDF contained no safely attributable target-model rows"
        )

    declared_urls = {
        item.get("url")
        for item in [
            *datasheets,
            *(
                decision.get("sources")
                if isinstance(decision.get("sources"), list)
                else []
            ),
        ]
        if isinstance(item, Mapping) and isinstance(item.get("url"), str)
    }
    operator_manufacturer = public_brand_alias(
        str(payload.get("brand_code") or "")
    )
    if trusted_domains and not operator_manufacturer:
        raise SourceVerificationError(
            "trusted catalogue-brand hydration requires a public manufacturer alias"
        )
    validation_base = {
        **dict(decision),
        "schema_version": "1",
        "product_id": product_id,
        "facts": [],
        "datasheet_parameters": [],
        "derived_insights": [],
        "classification_evidence_urls": [],
        "classification_evidence_quotes": [],
    }

    def validate_parameters(candidate_parameters: list[Any]) -> dict[str, Any]:
        return validate_decision(
            {
                **validation_base,
                "datasheet_parameters": candidate_parameters,
            },
            expected_product_id=product_id,
            expected_lease_token=str(decision.get("lease_token") or ""),
            minimum_confidence=min_publish_confidence(),
            minimum_fact_confidence=min_fact_confidence(),
            mirrors_allowed=allow_mirrors(),
            allowed_evidence_urls=set(declared_urls),
            allowed_classification_urls=set(declared_urls),
            trusted_source_domains=trusted_domains,
            operator_manufacturer_identity=operator_manufacturer or None,
            expected_product_name=str(payload.get("product_name") or ""),
            evidence_text_by_url={url: evidence.text},
            classification_text_by_url={url: evidence.text},
            verified_primary_document_urls={url},
        )

    validate_parameters([])
    retained_raw: list[Any] = []
    retained_normalized: list[dict[str, Any]] = []
    for parameter in bound_parameters[:_MAX_LOCAL_DATASHEET_PARAMETERS]:
        try:
            normalized = validate_parameters([*retained_raw, parameter])
        except DecisionError:
            continue
        retained_raw.append(parameter)
        retained_normalized = list(normalized["datasheet_parameters"])
    if not retained_normalized:
        raise CLIError(
            "primary PDF rows did not pass the publication evidence gate"
        )
    refreshed = {
        **dict(decision),
        "datasheet_parameters": retained_normalized,
    }
    return refreshed, {
        "url": url,
        "pdf_sha256": evidence.sha256,
        "page_count": evidence.page_count,
        "extracted_pages": evidence.extracted_pages,
        "parsed_parameter_count": len(row_payloads),
        "retained_parameter_count": len(retained_normalized),
    }


def _parameter_analysis_product(
    candidate: ContentRefreshCandidate,
    decision: Mapping[str, Any],
) -> dict[str, Any]:
    """Return only reader-facing identity fields for parameter analysis."""

    return {
        "name": str(candidate.payload.get("product_name") or ""),
        "model": str(decision.get("model") or ""),
        "manufacturer": str(decision.get("manufacturer") or ""),
        "product_category": str(decision.get("product_category") or ""),
        "product_type": str(decision.get("product_type") or ""),
    }


def _parameter_analysis_request_fingerprint(
    *,
    product: Mapping[str, Any],
    parameters: Sequence[Mapping[str, Any]],
    settings: AISettings,
) -> str:
    """Hash the exact model identity and safe request messages."""

    messages = build_parameter_analysis_messages(
        product=product,
        parameters=parameters,
        max_evidence_chars=settings.max_evidence_chars,
    )
    payload = {
        "schema_version": PARAMETER_ANALYSIS_SCHEMA_VERSION,
        "model": settings.model,
        "max_tokens": settings.max_tokens,
        "thinking_mode": settings.thinking_mode,
        "reasoning_effort": settings.reasoning_effort,
        "json_response_format": settings.json_response_format,
        "messages": messages,
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _enrich_refreshed_parameters(
    store: StateStore,
    candidate: ContentRefreshCandidate,
    decision: Mapping[str, Any],
    parameter_refresh: Mapping[str, Any],
    *,
    client: OpenAICompatibleClient | None,
    settings: AISettings | None,
    analyze: bool,
) -> tuple[dict[str, Any], dict[str, Any], int, int | None]:
    """Persist one verified set and optionally reuse or run grounded AI analysis."""

    raw_parameters = decision.get("datasheet_parameters")
    if not isinstance(raw_parameters, list) or not raw_parameters:
        raise CLIError("parameter enrichment requires verified datasheet rows")
    parameters: list[Mapping[str, Any]] = []
    for index, item in enumerate(raw_parameters):
        if not isinstance(item, Mapping):
            raise CLIError(
                f"verified datasheet parameter {index} must be an object"
            )
        parameters.append(item)
    url = parameter_refresh.get("url")
    pdf_sha256 = parameter_refresh.get("pdf_sha256")
    if not isinstance(url, str) or not isinstance(pdf_sha256, str):
        raise CLIError("parameter refresh is missing verified PDF provenance")
    parameter_set = store.record_verified_parameter_set(
        candidate,
        pdf_url=url,
        pdf_sha256=pdf_sha256,
        parameters=parameters,
        extractor_version=PDF_EXTRACTION_CONTRACT_VERSION,
        validation_policy_fingerprint=_validation_policy_fingerprint(),
    )
    metadata: dict[str, Any] = {
        "parameter_set_id": parameter_set.parameter_set_id,
        "parameter_count": parameter_set.parameter_count,
        "parameter_set_sha256": parameter_set.parameter_set_sha256,
        "extractor_version": parameter_set.extractor_version,
        "analysis_requested": analyze,
    }
    if not analyze:
        return (
            dict(decision),
            metadata,
            parameter_set.parameter_set_id,
            None,
        )
    if client is None or settings is None:
        raise CLIError("parameter analysis client is not configured")

    product = _parameter_analysis_product(candidate, decision)
    fingerprint = _parameter_analysis_request_fingerprint(
        product=product,
        parameters=parameters,
        settings=settings,
    )
    run = store.find_completed_parameter_analysis_run(
        parameter_set.parameter_set_id,
        fingerprint,
    )
    reused = run is not None
    if run is None:
        start = store.begin_parameter_analysis_run(
            parameter_set.parameter_set_id,
            request_fingerprint=fingerprint,
            prompt_version=PARAMETER_ANALYSIS_PROMPT_VERSION,
            glossary_version=PARAMETER_GLOSSARY_VERSION,
            model=settings.model,
        )
        run = start.record
        if not start.should_execute:
            if run.status != "completed" or run.analysis is None:
                raise StateError(
                    "an identical parameter analysis is already in progress"
                )
            reused = True
        else:
            try:
                analysis = client.analyze_parameters(
                    product=product,
                    parameters=parameters,
                )
            except (AIError, ParameterAnalysisError, TypeError, ValueError) as exc:
                usage = _ai_response_audit(client)
                usage["request_max_tokens"] = settings.max_tokens
                usage["request_thinking_mode"] = (
                    settings.thinking_mode or "default"
                )
                usage["provider_requests"] = _provider_request_count(
                    client,
                    "last_parameter_analysis_provider_requests",
                )
                store.fail_parameter_analysis_run(
                    run.analysis_run_id,
                    error=_safe_error(exc),
                    usage=usage,
                )
                raise
            usage = _ai_response_audit(client)
            usage["request_max_tokens"] = settings.max_tokens
            usage["request_thinking_mode"] = (
                settings.thinking_mode or "default"
            )
            usage["provider_requests"] = _provider_request_count(
                client,
                "last_parameter_analysis_provider_requests",
            )
            run = store.complete_parameter_analysis_run(
                run.analysis_run_id,
                analysis=analysis,
                usage=usage,
            )
    if run.analysis is None:
        raise StateError("completed parameter analysis has no result")
    translated = apply_parameter_translations(parameters, run.analysis)
    enriched_decision = {
        **dict(decision),
        "datasheet_parameters": translated,
        "professional_analysis": professional_analysis(
            run.analysis,
            parameters,
        ),
    }
    metadata.update(
        {
            "analysis_run_id": run.analysis_run_id,
            "analysis_request_fingerprint": fingerprint,
            "analysis_reused": reused,
            "prompt_version": run.prompt_version,
            "glossary_version": run.glossary_version,
            "model": run.model,
            "request_max_tokens": settings.max_tokens,
            "request_thinking_mode": settings.thinking_mode or "default",
            "input_parameter_count": len(parameters),
            "input_complete": True,
            "provider_requests": (
                0
                if reused
                else _provider_request_count(
                    client,
                    "last_parameter_analysis_provider_requests",
                )
            ),
            **(
                {"usage": run.usage}
                if isinstance(run.usage, Mapping)
                else {}
            ),
        }
    )
    return (
        enriched_decision,
        metadata,
        parameter_set.parameter_set_id,
        run.analysis_run_id,
    )


def _research_audit(
    *,
    model: str,
    search: Mapping[str, Any],
    extract: Mapping[str, Any],
    ai_calls: int,
    gaps: list[str],
    ai_provider_requests: int | None = None,
    stop_reason: str | None = None,
) -> dict[str, Any]:
    search_results = search.get("results")
    extract_results = extract.get("results")
    verified = [
        item
        for item in extract_results
        if isinstance(item, Mapping) and item.get("identity_verified") is True
    ] if isinstance(extract_results, list) else []
    payload: dict[str, Any] = {
        "ai": {
            "model": model,
            "calls": ai_calls,
            "provider_requests": (
                ai_calls
                if ai_provider_requests is None
                else ai_provider_requests
            ),
        },
        "research": {
            "gaps": gaps,
            "stop_reason": stop_reason,
        },
        "exa": {
            "search_usage": search.get("usage", {}),
            "extract_usage": extract.get("usage", {}),
            "query_count": len(search.get("queries", []))
            if isinstance(search.get("queries"), list)
            else 0,
            "candidate_count": len(search_results)
            if isinstance(search_results, list)
            else 0,
            "classification_evidence_count": len(extract_results)
            if isinstance(extract_results, list)
            else 0,
            "evidence_count": len(verified),
        },
    }
    return payload


def _finish_research_gap(
    store: StateStore,
    lease: Lease,
    *,
    gap: str | None,
    stop_reason: str,
    audit: dict[str, Any],
) -> dict[str, Any]:
    if gap == "independent_corroboration":
        outcome = store.record_outcome(
            lease,
            "source_unverified",
            error=(
                "bounded research ended before independent source "
                "corroboration was established"
            ),
            details={"automation": audit},
        )
        return {
            "ok": True,
            "processed": True,
            "published": False,
            "product_id": lease.product_id,
            "outcome": outcome.outcome,
            "next_attempt_at": outcome.next_attempt_at,
        }
    outcome = {
        "manufacturer_identity": "insufficient_identity",
        "scope_classification": "insufficient_identity",
        "conflict_resolution": "ambiguous",
        "primary_datasheet": "no_datasheet",
        "missing_exact_fact": "no_datasheet",
    }.get(gap or "", "no_datasheet")
    return _apply_decision(
        store,
        lease,
        _nonpublish_decision(
            lease,
            outcome,
            f"Bounded research stopped: {stop_reason}.",
        ),
        audit=audit,
    )


def _validate_global_research_limits(
    settings: GlobalResearchBudgetSettings,
    reserved: int,
) -> None:
    """Require every enabled global window to admit one complete product."""

    if (
        isinstance(reserved, bool)
        or not isinstance(reserved, int)
        or reserved < 0
    ):
        raise CLIError("global research budget reservation must be non-negative")
    for name, limit in (
        (
            "PV_WIKI_GLOBAL_DAILY_CREDIT_LIMIT",
            settings.daily_credit_limit,
        ),
        (
            "PV_WIKI_GLOBAL_MONTHLY_CREDIT_LIMIT",
            settings.monthly_credit_limit,
        ),
    ):
        if 0 < limit < reserved:
            raise ConfigError(
                f"{name} must be zero or at least "
                "PV_WIKI_RESEARCH_MAX_CREDITS"
            )


def _global_research_budget_pause(
    store: StateStore,
    *,
    now: datetime | None = None,
    reservation_credits: int | None = None,
) -> dict[str, Any] | None:
    """Return one fail-closed UTC stop-loss before paid research starts."""

    settings = GlobalResearchBudgetSettings.from_env()
    reserved = (
        ResearchSettings.from_env().max_credits
        if reservation_credits is None
        else reservation_credits
    )
    _validate_global_research_limits(settings, reserved)
    timestamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    day_start = timestamp.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    month_start = day_start.replace(day=1)
    month_end = next_month_start(timestamp)
    windows = (
        (
            "daily",
            settings.daily_credit_limit,
            day_start,
            day_end,
        ),
        (
            "monthly",
            settings.monthly_credit_limit,
            month_start,
            month_end,
        ),
    )
    blocked_windows: list[dict[str, Any]] = []
    for period, limit, start, end in windows:
        if limit <= 0:
            continue
        usage = store.research_usage_between(start=start, end=end)
        known_credits = float(usage["known_credits"])
        unknown_actions = int(usage["unknown_or_uncertain_actions"])
        reason: str | None = None
        if known_credits >= limit:
            reason = f"global_{period}_research_budget_exhausted"
        elif unknown_actions:
            reason = f"global_{period}_research_usage_uncertain"
        elif known_credits + reserved > limit:
            reason = f"global_{period}_research_budget_reservation_blocked"
        if reason is None:
            continue
        blocked_windows.append(
            {
                "period": period,
                "reason": reason,
                "known_credits": (
                    int(known_credits)
                    if known_credits.is_integer()
                    else known_credits
                ),
                "credit_limit": limit,
                "reserved_credits": reserved,
                "unknown_or_uncertain_actions": unknown_actions,
                "resume_at": end.isoformat(),
            }
        )
    if not blocked_windows:
        return None
    primary = blocked_windows[0]
    return {
        "ok": True,
        "processed": False,
        "published": False,
        **{
            key: value
            for key, value in primary.items()
            if key != "period"
        },
        "resume_at": max(
            str(window["resume_at"]) for window in blocked_windows
        ),
        "blocked_windows": [
            str(window["period"]) for window in blocked_windows
        ],
    }


def _defer_research_pause(
    store: StateStore,
    lease: Lease,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Release a lease without blaming the product for a system-wide pause."""

    reason = str(payload.get("reason") or "research_system_pause")
    store.defer_lease(lease, reason[:200])
    result = dict(payload)
    result.setdefault("pause_scope", "system")
    return result


def run_one(
    *,
    worker_id: str | None = None,
    lease_seconds: int = 3600,
    max_results: int = 5,
    search_timeout: float = 30.0,
) -> dict[str, Any]:
    """Run one product through a bounded, durable research feedback loop."""

    if (
        isinstance(max_results, bool)
        or not isinstance(max_results, int)
        or not 1 <= max_results <= 5
    ):
        raise CLIError("run-one max_results must be between 1 and 5")
    if (
        isinstance(lease_seconds, bool)
        or not isinstance(lease_seconds, int)
        or not 300 <= lease_seconds <= 7200
    ):
        raise CLIError("run-one lease_seconds must be between 300 and 7200")
    if (
        isinstance(search_timeout, bool)
        or not isinstance(search_timeout, (int, float))
        or not 1 <= float(search_timeout) <= 120
    ):
        raise CLIError("run-one search timeout must be between 1 and 120 seconds")

    research_settings = ResearchSettings.from_env()
    required_lease_seconds = math.ceil(
        research_settings.max_seconds + RESEARCH_LEASE_TAIL_SECONDS
    )
    if lease_seconds < required_lease_seconds:
        raise CLIError(
            "run-one lease_seconds must be at least "
            f"{required_lease_seconds} for the configured research deadline "
            "and one final bounded provider call"
        )
    with _store() as store:
        if not store.list_due(limit=1):
            return {
                "ok": True,
                "processed": False,
                "reason": "no_due_product",
            }
        lease = store.lease_next(
            _worker_id(worker_id),
            lease_seconds=lease_seconds,
        )
        if lease is None:
            return {
                "ok": True,
                "processed": False,
                "reason": "no_due_product",
            }

        try:
            content_failure_streak = store.content_failure_streak(
                lease.product_id
            )
            if (
                content_failure_streak
                >= CONTENT_FAILURE_QUARANTINE_THRESHOLD
            ):
                outcome = store.record_outcome(
                    lease,
                    "content_quarantined",
                    error=(
                        "repeated bounded content failures for the unchanged "
                        "catalogue source revision"
                    ),
                    details={
                        "automation": {
                            "content_failure_streak":
                                content_failure_streak,
                            "threshold":
                                CONTENT_FAILURE_QUARANTINE_THRESHOLD,
                            "wake_policy":
                                "source_change_or_annual_refresh",
                        }
                    },
                )
                return {
                    "ok": True,
                    "processed": True,
                    "published": False,
                    "product_id": lease.product_id,
                    "outcome": outcome.outcome,
                    "next_attempt_at": outcome.next_attempt_at,
                    "reason": "content_failure_quarantine",
                }

            try:
                search_identity = _search_identity(lease.payload)
            except CLIError:
                return _apply_decision(
                    store,
                    lease,
                    _nonpublish_decision(
                        lease,
                        "insufficient_identity",
                        "The catalogue record has no public product name.",
                    ),
                )
            product_name = " ".join(
                str(lease.payload.get("product_name") or "").split()
            )
            product_model = str(search_identity["model"])
            research_identity = " ".join(
                dict.fromkeys(
                    item
                    for item in (product_model, product_name)
                    if item
                )
            )

            recovered_publish = _recover_validated_publish(store, lease)
            if recovered_publish is not None:
                recovered_decision, recovered_attempt_id = recovered_publish
                return _apply_decision(
                    store,
                    lease,
                    recovered_decision,
                    validated_decision=recovered_decision,
                    audit={
                        "recovered_publish_attempt_id": recovered_attempt_id,
                        "research_reused": True,
                    },
                )

            # Everything below this point can initiate paid research. Systemic
            # gates therefore release this lease back to its exact prior queue
            # state instead of assigning a failure to the selected product.
            invalid_streak = store.recent_distinct_outcome_streak(
                "invalid_decision",
                limit=INVALID_DECISION_CIRCUIT_THRESHOLD,
                within=INVALID_DECISION_CIRCUIT_WINDOW,
            )
            if invalid_streak >= INVALID_DECISION_CIRCUIT_THRESHOLD:
                return _defer_research_pause(
                    store,
                    lease,
                    {
                        "ok": True,
                        "processed": False,
                        "published": False,
                        "reason": "ai_decision_circuit_open",
                        "distinct_products": invalid_streak,
                    },
                )

            try:
                budget_pause = _global_research_budget_pause(
                    store,
                    reservation_credits=research_settings.max_credits,
                )
            except ConfigError as exc:
                store.defer_lease(
                    lease,
                    (
                        "research_configuration_"
                        f"{exc.__class__.__name__}"
                    )[:200],
                )
                raise
            if budget_pause is not None:
                return _defer_research_pause(store, lease, budget_pause)

            try:
                ai_settings = AISettings.from_env()
            except (AIError, ConfigError) as exc:
                store.defer_lease(
                    lease,
                    f"research_configuration_{exc.__class__.__name__}"[:200],
                )
                raise

            ai_provider_fingerprint = _ai_provider_fingerprint(ai_settings)
            ai_provider_event = store.recent_ai_provider_rejection_event(
                ai_provider_fingerprint,
                http_statuses=AI_PROVIDER_CIRCUIT_HTTP_STATUSES,
                within=AI_PROVIDER_CIRCUIT_WINDOW,
            )
            ai_provider_reason = "definitive_rejection"
            if ai_provider_event is None:
                ai_provider_event = store.recent_ai_provider_rejection_event(
                    ai_provider_fingerprint,
                    http_statuses=AI_RATE_LIMIT_CIRCUIT_HTTP_STATUSES,
                    within=AI_RATE_LIMIT_CIRCUIT_WINDOW,
                )
                ai_provider_reason = "rate_limit"
            if ai_provider_event is not None:
                return _defer_research_pause(
                    store,
                    lease,
                    {
                        "ok": True,
                        "processed": False,
                        "published": False,
                        "reason": "ai_provider_circuit_open",
                        "circuit_reason": ai_provider_reason,
                        "http_status": ai_provider_event.http_status,
                        "resume_at": (
                            ai_provider_event.expires_at.isoformat()
                        ),
                    },
                )

            invalid_output_products = (
                store.recent_ai_provider_error_products(
                    _ai_output_fingerprint(ai_settings),
                    error_types=AI_INVALID_OUTPUT_CIRCUIT_ERROR_TYPES,
                    categories=AI_INVALID_OUTPUT_CIRCUIT_CATEGORIES,
                    within=AI_INVALID_OUTPUT_CIRCUIT_WINDOW,
                )
            )
            if (
                invalid_output_products
                >= AI_INVALID_OUTPUT_CIRCUIT_THRESHOLD
            ):
                return _defer_research_pause(
                    store,
                    lease,
                    {
                        "ok": True,
                        "processed": False,
                        "published": False,
                        "reason": "ai_invalid_output_circuit_open",
                        "distinct_products": invalid_output_products,
                    },
                )

            try:
                search_client = _make_search_client(timeout=search_timeout)
            except (ConfigError, ExaError) as exc:
                store.defer_lease(
                    lease,
                    f"research_configuration_{exc.__class__.__name__}"[:200],
                )
                raise

            exa_provider_fingerprint = _exa_provider_fingerprint(
                search_client
            )
            exa_provider_event = store.recent_exa_provider_rejection(
                exa_provider_fingerprint,
                http_statuses=EXA_PROVIDER_CIRCUIT_HTTP_STATUSES,
                within=EXA_PROVIDER_CIRCUIT_WINDOW,
            )
            exa_provider_reason = "definitive_rejection"
            if exa_provider_event is None:
                exa_provider_event = store.recent_exa_provider_rejection(
                    exa_provider_fingerprint,
                    http_statuses=EXA_RATE_LIMIT_CIRCUIT_HTTP_STATUSES,
                    within=EXA_RATE_LIMIT_CIRCUIT_WINDOW,
                )
                exa_provider_reason = "rate_limit"
            if exa_provider_event is not None:
                return _defer_research_pause(
                    store,
                    lease,
                    {
                        "ok": True,
                        "processed": False,
                        "published": False,
                        "reason": "exa_provider_circuit_open",
                        "circuit_reason": exa_provider_reason,
                        "http_status": exa_provider_event.http_status,
                        "resume_at": (
                            exa_provider_event.expires_at.isoformat()
                        ),
                    },
                )

            now = datetime.now(timezone.utc)
            month_start = datetime(
                now.year,
                now.month,
                1,
                tzinfo=timezone.utc,
            )
            quota_event = store.recent_exa_provider_error(
                exa_provider_fingerprint,
                error_types={"ExaQuotaExhaustedError"},
                within=max(now - month_start, timedelta(microseconds=1)),
                now=now,
            )
            if (
                quota_event is not None
                and quota_event.finished_at >= month_start
            ):
                return _defer_research_pause(
                    store,
                    lease,
                    {
                        "ok": True,
                        "processed": False,
                        "published": False,
                        "reason": "search_quota_exhausted",
                        "provider": "exa",
                        "resume_at": next_month_start(now).isoformat(),
                    },
                )

            try:
                ai_client = OpenAICompatibleClient(ai_settings)
                research_scope_fingerprints = _research_scope_fingerprints(
                    lease.payload,
                    ai_settings,
                    search_client,
                    max_results=max_results,
                    research_settings=research_settings,
                )
            except (AIError, ConfigError, ExaError) as exc:
                store.defer_lease(
                    lease,
                    f"research_configuration_{exc.__class__.__name__}"[:200],
                )
                raise

            started = time.monotonic()
            search: dict[str, Any] = {
                "queries": [],
                "results": [],
                "usage": {"credits": 0},
                "request_ids": [],
            }
            extract: dict[str, Any] = {
                "query": "",
                "results": [],
                "failed_results": [],
                "usage": {"credits": 0},
            }
            query_history: list[str] = []
            submitted_urls: set[str] = set()
            gaps: list[str] = []
            ai_calls = 0
            ai_provider_requests = 0
            last_gap: str | None = None
            pending_queries: tuple[str, ...] | None = None
            validation_feedback: ValidationFeedback | None = None
            trusted_source_policy = _trusted_source_policy_for_product(
                lease.payload
            )
            candidate_manufacturer: str | None = (
                trusted_source_policy.manufacturer
                if trusted_source_policy is not None
                else None
            )
            search_budget_units = 0.0

            initial_search = _run_research_search(
                store,
                lease,
                search_client,
                round_number=0,
                max_results=max_results,
                scope_fingerprints=research_scope_fingerprints,
            )
            search = _merge_search_bundles(search, initial_search)
            query_history.extend(initial_search.get("queries", []))
            search_budget_units += max(
                _credits(initial_search),
                SEARCH_BUDGET_UNITS_PER_QUERY
                * len(initial_search.get("queries", [])),
            )

            initial_candidates: list[str] = []
            for item in initial_search.get("results", []):
                url = item.get("url") if isinstance(item, Mapping) else None
                if (
                    isinstance(url, str)
                    and url not in submitted_urls
                    and url not in initial_candidates
                ):
                    initial_candidates.append(url)
            initial_extract_limit = max(
                1,
                MAX_RESEARCH_EVIDENCE_URLS
                - (research_settings.max_rounds - 1),
            )
            initial_urls = initial_candidates[:initial_extract_limit]
            if (
                initial_urls
                and (
                    search_budget_units
                    + EXTRACT_BUDGET_UNITS_PER_BATCH
                    <= research_settings.max_credits
                )
                and time.monotonic() - started < research_settings.max_seconds
            ):
                submitted_urls.update(initial_urls)
                initial_extract = _run_research_extract(
                    store,
                    lease,
                    search_client,
                    round_number=0,
                    scope_fingerprints=research_scope_fingerprints,
                    urls=initial_urls,
                    query=_research_extract_query(research_identity, None),
                )
                extract = _merge_extract_bundles(extract, initial_extract)
                search_budget_units += max(
                    _credits(initial_extract),
                    EXTRACT_BUDGET_UNITS_PER_BATCH,
                )

            round_number = 0
            while round_number < research_settings.max_rounds:
                if pending_queries is not None:
                    stop_reason: str | None = None
                    if (
                        len(query_history) + len(pending_queries)
                        > research_settings.max_queries
                    ):
                        stop_reason = "query budget exhausted"
                    elif (
                        search_budget_units
                        + (
                            SEARCH_BUDGET_UNITS_PER_QUERY
                            * len(pending_queries)
                        )
                        > research_settings.max_credits
                    ):
                        stop_reason = (
                            "Search budget cannot reserve the next search"
                        )
                    elif (
                        time.monotonic() - started
                        >= research_settings.max_seconds
                    ):
                        stop_reason = "research wall-clock budget exhausted"
                    if stop_reason is not None:
                        audit = _research_audit(
                            model=ai_settings.model,
                            search=search,
                            extract=extract,
                            ai_calls=ai_calls,
                            gaps=gaps,
                            ai_provider_requests=ai_provider_requests,
                            stop_reason=stop_reason,
                        )
                        return _finish_research_gap(
                            store,
                            lease,
                            gap=last_gap,
                            stop_reason=stop_reason,
                            audit=audit,
                        )

                    supplemental_search = _run_research_search(
                        store,
                        lease,
                        search_client,
                        round_number=round_number,
                        max_results=max_results,
                        scope_fingerprints=research_scope_fingerprints,
                        queries=pending_queries,
                    )
                    search = _merge_search_bundles(
                        search,
                        supplemental_search,
                    )
                    search_budget_units += max(
                        _credits(supplemental_search),
                        SEARCH_BUDGET_UNITS_PER_QUERY
                        * len(pending_queries),
                    )
                    query_history.extend(pending_queries)
                    if (
                        time.monotonic() - started
                        >= research_settings.max_seconds
                    ):
                        stop_reason = (
                            "research wall-clock budget exhausted after search"
                        )
                        audit = _research_audit(
                            model=ai_settings.model,
                            search=search,
                            extract=extract,
                            ai_calls=ai_calls,
                            gaps=gaps,
                            ai_provider_requests=ai_provider_requests,
                            stop_reason=stop_reason,
                        )
                        return _finish_research_gap(
                            store,
                            lease,
                            gap=last_gap,
                            stop_reason=stop_reason,
                            audit=audit,
                        )
                    new_candidates: list[str] = []
                    for item in supplemental_search.get("results", []):
                        url = item.get("url") if isinstance(item, Mapping) else None
                        if (
                            isinstance(url, str)
                            and url not in submitted_urls
                            and url not in new_candidates
                        ):
                            new_candidates.append(url)
                    if not new_candidates:
                        stop_reason = "supplemental search returned no new URL"
                        audit = _research_audit(
                            model=ai_settings.model,
                            search=search,
                            extract=extract,
                            ai_calls=ai_calls,
                            gaps=gaps,
                            ai_provider_requests=ai_provider_requests,
                            stop_reason=stop_reason,
                        )
                        return _finish_research_gap(
                            store,
                            lease,
                            gap=last_gap,
                            stop_reason=stop_reason,
                            audit=audit,
                        )

                    remaining_capacity = (
                        MAX_RESEARCH_EVIDENCE_URLS - len(submitted_urls)
                    )
                    if remaining_capacity <= 0:
                        stop_reason = "extract URL budget exhausted"
                        audit = _research_audit(
                            model=ai_settings.model,
                            search=search,
                            extract=extract,
                            ai_calls=ai_calls,
                            gaps=gaps,
                            ai_provider_requests=ai_provider_requests,
                            stop_reason=stop_reason,
                        )
                        return _finish_research_gap(
                            store,
                            lease,
                            gap=last_gap,
                            stop_reason=stop_reason,
                            audit=audit,
                        )
                    if (
                        search_budget_units
                        + EXTRACT_BUDGET_UNITS_PER_BATCH
                        > research_settings.max_credits
                    ):
                        stop_reason = (
                            "Search budget cannot reserve the next extract"
                        )
                        audit = _research_audit(
                            model=ai_settings.model,
                            search=search,
                            extract=extract,
                            ai_calls=ai_calls,
                            gaps=gaps,
                            ai_provider_requests=ai_provider_requests,
                            stop_reason=stop_reason,
                        )
                        return _finish_research_gap(
                            store,
                            lease,
                            gap=last_gap,
                            stop_reason=stop_reason,
                            audit=audit,
                        )
                    future_supplemental_rounds = (
                        research_settings.max_rounds - round_number - 1
                    )
                    extract_limit = max(
                        1,
                        remaining_capacity - future_supplemental_rounds,
                    )
                    supplemental_urls = new_candidates[
                        : min(remaining_capacity, extract_limit)
                    ]
                    previous_content_urls = {
                        item.get("url")
                        for item in extract.get("results", [])
                        if isinstance(item, Mapping)
                        and isinstance(item.get("raw_content"), str)
                        and item["raw_content"].strip()
                    }
                    submitted_urls.update(supplemental_urls)
                    supplemental_extract = _run_research_extract(
                        store,
                        lease,
                        search_client,
                        round_number=round_number,
                        scope_fingerprints=research_scope_fingerprints,
                        urls=supplemental_urls,
                        query=_research_extract_query(
                            research_identity,
                            last_gap,
                        ),
                    )
                    extract = _merge_extract_bundles(
                        extract,
                        supplemental_extract,
                    )
                    search_budget_units += max(
                        _credits(supplemental_extract),
                        EXTRACT_BUDGET_UNITS_PER_BATCH,
                    )
                    new_content = [
                        item
                        for item in supplemental_extract.get("results", [])
                        if isinstance(item, Mapping)
                        and isinstance(item.get("url"), str)
                        and item["url"] not in previous_content_urls
                        and isinstance(item.get("raw_content"), str)
                        and item["raw_content"].strip()
                    ]
                    if not new_content:
                        stop_reason = (
                            "supplemental extract returned no new usable evidence"
                        )
                        audit = _research_audit(
                            model=ai_settings.model,
                            search=search,
                            extract=extract,
                            ai_calls=ai_calls,
                            gaps=gaps,
                            ai_provider_requests=ai_provider_requests,
                            stop_reason=stop_reason,
                        )
                        return _finish_research_gap(
                            store,
                            lease,
                            gap=last_gap,
                            stop_reason=stop_reason,
                            audit=audit,
                        )
                    pending_queries = None
                    validation_feedback = None

                if (
                    time.monotonic() - started
                    >= research_settings.max_seconds
                ):
                    stop_reason = "research wall-clock budget exhausted before AI"
                    audit = _research_audit(
                        model=ai_settings.model,
                        search=search,
                        extract=extract,
                        ai_calls=ai_calls,
                        gaps=gaps,
                        ai_provider_requests=ai_provider_requests,
                        stop_reason=stop_reason,
                    )
                    return _finish_research_gap(
                        store,
                        lease,
                        gap=last_gap,
                        stop_reason=stop_reason,
                        audit=audit,
                    )

                final_only = (
                    round_number + 1 >= research_settings.max_rounds
                )
                action, provider_requests = _run_research_ai(
                    store,
                    lease,
                    ai_client,
                    ai_settings,
                    round_number=round_number,
                    scope_fingerprints=research_scope_fingerprints,
                    search=search,
                    extract=extract,
                    candidate_manufacturer=candidate_manufacturer,
                    previous_queries=tuple(query_history),
                    validation_feedback=validation_feedback,
                    trusted_source_policy=trusted_source_policy,
                    final_only=final_only,
                )
                ai_calls += 1
                ai_provider_requests += provider_requests

                if isinstance(action, SearchMoreAction):
                    last_gap = action.gap.value
                    gaps.append(last_gap)
                    if round_number + 1 >= research_settings.max_rounds:
                        stop_reason = "research round budget exhausted"
                        audit = _research_audit(
                            model=ai_settings.model,
                            search=search,
                            extract=extract,
                            ai_calls=ai_calls,
                            gaps=gaps,
                            ai_provider_requests=ai_provider_requests,
                            stop_reason=stop_reason,
                        )
                        return _finish_research_gap(
                            store,
                            lease,
                            gap=last_gap,
                            stop_reason=stop_reason,
                            audit=audit,
                        )
                    pending_queries = action.queries
                    validation_feedback = None
                    round_number += 1
                    continue

                proposal = action.decision
                proposed_manufacturer = proposal.get("manufacturer")
                if (
                    isinstance(proposed_manufacturer, str)
                    and proposed_manufacturer.strip()
                ):
                    candidate_manufacturer = proposed_manufacturer.strip()[:300]
                proposed_outcome = proposal.get("outcome")
                if proposed_outcome in {
                    "no_datasheet",
                    "ambiguous",
                    "insufficient_identity",
                }:
                    # These outcomes cannot publish anything. Build their
                    # payload locally so contradictory model-authored primary
                    # datasheets, community sources, or facts cannot turn a
                    # safe negative conclusion into invalid_decision.
                    raw = _nonpublish_decision(
                        lease,
                        proposed_outcome,
                        (
                            "AI returned a conservative non-publish decision; "
                            "runtime discarded all publication fields."
                        ),
                    )
                else:
                    raw = {
                        **proposal,
                        "schema_version": "1",
                        "product_id": lease.product_id,
                        "lease_token": lease.token,
                    }
                raw = _with_locally_bound_datasheet_parameters(raw, extract)
                (
                    evidence_text_by_url,
                    allowed_classification_urls,
                    classification_text_by_url,
                    verified_primary_document_urls,
                ) = _research_evidence_context(store, lease, extract)
                fact_diagnostics: dict[str, Any] = {}
                try:
                    validated = _validate_decision_for_lease(
                        store,
                        lease,
                        raw,
                        evidence_text_by_url=evidence_text_by_url,
                        allowed_classification_urls=allowed_classification_urls,
                        classification_text_by_url=classification_text_by_url,
                        verified_primary_document_urls=(
                            verified_primary_document_urls
                        ),
                        fact_diagnostics=fact_diagnostics,
                    )
                except DecisionError as exc:
                    recoverable_gap = _validation_research_gap(exc)
                    if recoverable_gap is None:
                        return _apply_decision(
                            store,
                            lease,
                            raw,
                            evidence_text_by_url=evidence_text_by_url,
                            allowed_classification_urls=allowed_classification_urls,
                            classification_text_by_url=classification_text_by_url,
                            verified_primary_document_urls=(
                                verified_primary_document_urls
                            ),
                            audit=_research_audit(
                                model=ai_settings.model,
                                search=search,
                                extract=extract,
                                ai_calls=ai_calls,
                                gaps=gaps,
                                ai_provider_requests=ai_provider_requests,
                            ),
                        )
                    last_gap = recoverable_gap
                    gaps.append(recoverable_gap)
                    if (
                        validation_feedback is not None
                        and validation_feedback.gap.value
                        == recoverable_gap
                    ):
                        stop_reason = (
                            "local validation gap repeated after one bounded "
                            "AI repair without new evidence"
                        )
                        audit = _research_audit(
                            model=ai_settings.model,
                            search=search,
                            extract=extract,
                            ai_calls=ai_calls,
                            gaps=gaps,
                            ai_provider_requests=ai_provider_requests,
                            stop_reason=stop_reason,
                        )
                        return _finish_research_gap(
                            store,
                            lease,
                            gap=recoverable_gap,
                            stop_reason=stop_reason,
                            audit=audit,
                        )
                    if round_number + 1 >= research_settings.max_rounds:
                        stop_reason = (
                            "local validation gap remained at the round limit"
                        )
                        audit = _research_audit(
                            model=ai_settings.model,
                            search=search,
                            extract=extract,
                            ai_calls=ai_calls,
                            gaps=gaps,
                            ai_provider_requests=ai_provider_requests,
                            stop_reason=stop_reason,
                        )
                        return _finish_research_gap(
                            store,
                            lease,
                            gap=recoverable_gap,
                            stop_reason=stop_reason,
                            audit=audit,
                        )
                    validation_feedback = ValidationFeedback.for_gap(
                        ResearchGap(recoverable_gap)
                    )
                    pending_queries = None
                    round_number += 1
                    continue

                audit = _research_audit(
                    model=ai_settings.model,
                    search=search,
                    extract=extract,
                    ai_calls=ai_calls,
                    gaps=gaps,
                    ai_provider_requests=ai_provider_requests,
                )
                return _apply_decision(
                    store,
                    lease,
                    raw,
                    evidence_text_by_url=evidence_text_by_url,
                    allowed_classification_urls=allowed_classification_urls,
                    classification_text_by_url=classification_text_by_url,
                    verified_primary_document_urls=(
                        verified_primary_document_urls
                    ),
                    audit=audit,
                    validated_decision=validated,
                    fact_diagnostics=fact_diagnostics,
                )

            stop_reason = "research loop ended without a final decision"
            return _finish_research_gap(
                store,
                lease,
                gap=last_gap,
                stop_reason=stop_reason,
                audit=_research_audit(
                    model=ai_settings.model,
                    search=search,
                    extract=extract,
                    ai_calls=ai_calls,
                    gaps=gaps,
                    ai_provider_requests=ai_provider_requests,
                    stop_reason=stop_reason,
                ),
            )
        except ResearchReplaySuppressedError as exc:
            stop_reason = str(exc)
            audit = _research_audit(
                model=ai_settings.model,
                search=search,
                extract=extract,
                ai_calls=ai_calls,
                gaps=gaps,
                ai_provider_requests=ai_provider_requests,
                stop_reason=stop_reason,
            )
            outcome = store.record_outcome(
                lease,
                "research_uncertain",
                error=stop_reason,
                details={"automation": audit},
            )
            return {
                "ok": True,
                "processed": True,
                "published": False,
                "product_id": lease.product_id,
                "outcome": outcome.outcome,
                "next_attempt_at": outcome.next_attempt_at,
                "reason": "uncertain paid request replay suppressed",
            }
        except ExaQuotaExhaustedError:
            return {
                "ok": True,
                "processed": False,
                "published": False,
                "reason": "search_quota_exhausted",
                "provider": "exa",
                "pause_scope": "product_and_provider",
                "resume_at": next_month_start().isoformat(),
            }

        except AITimeoutError as exc:
            # _run_research_ai already audits the uncertain provider action and
            # closes the product attempt as ai_error. Retry the product later,
            # but let n8n continue the remaining products in this batch.
            _record_active_failure(store, lease, "ai_error", exc)
            current = store.get_product(lease.product_id)
            result: dict[str, Any] = {
                "ok": True,
                "processed": True,
                "published": False,
                "product_id": lease.product_id,
                "outcome": "ai_error",
                "reason": "ai_timeout",
            }
            if current is not None:
                result["next_attempt_at"] = current.next_run_at
            return result
        except AIInvalidOutputError as exc:
            # The client has already exhausted its bounded repair/continuation
            # attempts and _run_research_ai has audited the failed action. This
            # is product-scoped model output, not a worker-wide outage: back
            # the product off and allow n8n to continue the remaining batch.
            _record_active_failure(store, lease, "ai_error", exc)
            current = store.get_product(lease.product_id)
            result = {
                "ok": True,
                "processed": True,
                "published": False,
                "product_id": lease.product_id,
                "outcome": "ai_error",
                "reason": f"ai_{exc.category.value}",
            }
            if current is not None:
                result["next_attempt_at"] = current.next_run_at
            return result
        except LeaseLostError as exc:
            _record_active_failure(store, lease, "error", exc)
            raise
        except (
            AIError,
            ConfigError,
            DecisionError,
            StateError,
            ExaError,
            WikiJSError,
            CLIError,
        ) as exc:
            _record_active_failure(store, lease, "error", exc)
            raise
        except Exception as exc:
            _record_active_failure(store, lease, "error", exc)
            raise


def _cmd_run_one(args: argparse.Namespace) -> int:
    _emit(
        run_one(
            worker_id=args.worker_id,
            lease_seconds=args.lease_seconds,
            max_results=args.max_results,
            search_timeout=args.search_timeout,
        )
    )
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    configured = WorkerSettings.from_env()
    settings = WorkerSettings(
        token=configured.token,
        host=args.host or configured.host,
        port=args.port or configured.port,
    )
    serve(settings)
    return 0


def _cmd_fail(args: argparse.Namespace) -> int:
    with _store() as store:
        lease = _require_lease(store, args.lease_token)
        outcome = store.record_outcome(
            lease,
            "error",
            error=args.reason.strip()[:1000],
        )
    _emit(
        {
            "ok": True,
            "product_id": lease.product_id,
            "outcome": outcome.outcome,
            "next_attempt_at": outcome.next_attempt_at,
        }
    )
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    with _store() as store:
        result: dict[str, Any] = {
            "ok": True,
            "state_backend": store.backend,
            "state_location": store.location,
            "schema_version": store.schema_version,
            "counts": store.status_counts(),
            "outcomes": store.outcome_counts(),
            "research": store.research_action_stats(),
            "due_now": len(store.list_due(limit=1_000_000)),
        }
        if args.product_id:
            product = store.get_product(args.product_id)
            result["product"] = product
            result["attempts"] = store.attempt_history(args.product_id)[-20:]
            result["research_actions"] = store.research_action_history(
                product_id=args.product_id,
                limit=100,
            )
    _emit(result)
    return 0


def _operator_datetime(value: str | None, *, name: str) -> datetime | None:
    if value is None:
        return None
    candidate = value.strip()
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise CLIError(f"{name} must be an ISO-8601 datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CLIError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _cmd_requeue(args: argparse.Namespace) -> int:
    attempted_after = _operator_datetime(
        args.attempted_after,
        name="--attempted-after",
    )
    attempted_before = _operator_datetime(
        args.attempted_before,
        name="--attempted-before",
    )
    with _store() as store:
        count = store.requeue_products(
            args.outcome,
            reason=args.reason,
            attempted_after=attempted_after,
            attempted_before=attempted_before,
            limit=args.limit,
        )
    _emit(
        {
            "ok": True,
            "requeued": count,
            "outcomes": list(dict.fromkeys(args.outcome)),
            "reason": " ".join(args.reason.split()),
            "attempted_after": attempted_after,
            "attempted_before": attempted_before,
        }
    )
    return 0


def _cmd_publish_home(_args: argparse.Namespace) -> int:
    _emit(publish_home())
    return 0


def _same_content_refresh_candidate(
    expected: ContentRefreshCandidate,
    current: ContentRefreshCandidate,
) -> bool:
    return (
        current.product_id == expected.product_id
        and current.source_attempt_id == expected.source_attempt_id
        and current.source_hash == expected.source_hash
        and current.previous_content_schema_version
        == expected.previous_content_schema_version
        and current.wiki_path == expected.wiki_path
    )


def _cmd_refresh_content(args: argparse.Namespace) -> int:
    if args.refresh_home and not args.apply:
        raise CLIError("--refresh-home requires --apply")
    if args.analyze_parameters and not args.hydrate_parameters:
        raise CLIError("--analyze-parameters requires --hydrate-parameters")

    wiki_settings: WikiSettings | None = None
    wiki_client: WikiJSClient | None = None
    if args.apply:
        wiki_settings = WikiSettings.from_env()
        wiki_client = WikiJSClient(
            wiki_settings.base_url,
            wiki_settings.token,
            timeout=wiki_settings.timeout,
            new_page_private=wiki_settings.new_page_private,
            new_page_published=wiki_settings.new_page_published,
        )

    refreshed: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    with _store() as store:
        candidates = store.content_refresh_candidates(
            CONTENT_SCHEMA_VERSION,
            product_ids=args.product_id,
            limit=args.limit,
        )
        preview = [
            {
                "product_id": candidate.product_id,
                "path": candidate.wiki_path,
                "source_attempt_id": candidate.source_attempt_id,
                "previous_content_schema_version": (
                    candidate.previous_content_schema_version
                ),
                "fact_diagnostics": candidate.fact_diagnostics,
            }
            for candidate in candidates
        ]
        if not args.apply:
            if args.hydrate_parameters:
                for candidate, item in zip(candidates, preview, strict=True):
                    try:
                        _, parameter_refresh = (
                            _refresh_decision_parameters_from_primary_pdf(
                                candidate.product_id,
                                candidate.payload,
                                candidate.decision,
                            )
                        )
                        item["datasheet_parameter_refresh"] = parameter_refresh
                        if args.analyze_parameters:
                            item["parameter_analysis"] = {
                                "planned": True,
                                "input_parameter_count": parameter_refresh[
                                    "retained_parameter_count"
                                ],
                                "input_complete": True,
                            }
                    except (
                        CLIError,
                        ConfigError,
                        DecisionError,
                        PDFDocumentError,
                        TypeError,
                        ValueError,
                    ) as exc:
                        failures.append(
                            {
                                "product_id": candidate.product_id,
                                "path": candidate.wiki_path,
                                "error": _safe_error(exc),
                            }
                        )
            _emit(
                {
                    "ok": not failures,
                    "apply": False,
                    "hydrate_parameters": bool(args.hydrate_parameters),
                    "analyze_parameters": bool(args.analyze_parameters),
                    "content_schema_version": CONTENT_SCHEMA_VERSION,
                    "candidate_count": len(candidates),
                    "candidates": preview,
                    "failed_count": len(failures),
                    "failures": failures,
                }
            )
            return 0 if not failures else 1

        assert wiki_settings is not None
        assert wiki_client is not None
        analysis_settings: AISettings | None = None
        analysis_client: OpenAICompatibleClient | None = None
        if args.analyze_parameters and candidates:
            configured_analysis_settings = AISettings.from_env()
            analysis_settings = (
                replace(
                    configured_analysis_settings,
                    thinking_mode="disabled",
                    reasoning_effort=None,
                )
                if configured_analysis_settings.thinking_mode == "enabled"
                else configured_analysis_settings
            )
            analysis_client = OpenAICompatibleClient(analysis_settings)
        for candidate in candidates:
            try:
                render_decision = candidate.decision
                parameter_refresh: dict[str, Any] | None = None
                parameter_enrichment: dict[str, Any] | None = None
                parameter_set_id: int | None = None
                analysis_run_id: int | None = None
                if args.hydrate_parameters:
                    render_decision, parameter_refresh = (
                        _refresh_decision_parameters_from_primary_pdf(
                            candidate.product_id,
                            candidate.payload,
                            candidate.decision,
                        )
                    )
                    preflight = store.content_refresh_candidates(
                        CONTENT_SCHEMA_VERSION,
                        product_ids=[candidate.product_id],
                        limit=1,
                    )
                    if (
                        not preflight
                        or not _same_content_refresh_candidate(
                            candidate,
                            preflight[0],
                        )
                    ):
                        raise StateError(
                            "content refresh candidate changed during parameter "
                            "preparation"
                        )
                    candidate = preflight[0]
                    (
                        render_decision,
                        parameter_enrichment,
                        parameter_set_id,
                        analysis_run_id,
                    ) = _enrich_refreshed_parameters(
                        store,
                        candidate,
                        render_decision,
                        parameter_refresh,
                        client=analysis_client,
                        settings=analysis_settings,
                        analyze=bool(args.analyze_parameters),
                    )
                with _SOURCE_PUBLISH_FENCE, store.publication_fence():
                    current = store.content_refresh_candidates(
                        CONTENT_SCHEMA_VERSION,
                        product_ids=[candidate.product_id],
                        limit=1,
                    )
                    if (
                        not current
                        or not _same_content_refresh_candidate(
                            candidate,
                            current[0],
                        )
                    ):
                        raise StateError(
                            "content refresh candidate changed before Wiki update"
                        )
                    candidate = current[0]
                    fields = _verified_wiki_page_fields(
                        candidate.product_id,
                        candidate.payload,
                        render_decision,
                        checked_at=candidate.verified_at,
                    )
                    result = wiki_client.update_existing_page(
                        candidate.wiki_path,
                        wiki_settings.locale,
                        fields["title"],
                        fields["description"],
                        fields["managed_content"],
                        fields["tags"],
                    )
                    event = store.record_content_refresh(
                        candidate,
                        CONTENT_SCHEMA_VERSION,
                        wiki_action=str(result.get("action") or ""),
                        fact_diagnostics=candidate.fact_diagnostics,
                        parameter_set_id=parameter_set_id,
                        analysis_run_id=analysis_run_id,
                    )
                page = (
                    result.get("page")
                    if isinstance(result.get("page"), Mapping)
                    else {}
                )
                refreshed.append(
                    {
                        "product_id": candidate.product_id,
                        "path": candidate.wiki_path,
                        "page_id": page.get("id"),
                        "wiki_action": event.wiki_action,
                        "source_attempt_id": event.source_attempt_id,
                        "previous_content_schema_version": (
                            event.previous_content_schema_version
                        ),
                        "content_schema_version": (
                            event.content_schema_version
                        ),
                        "fact_diagnostics": event.fact_diagnostics,
                        **(
                            {"datasheet_parameter_refresh": parameter_refresh}
                            if parameter_refresh is not None
                            else {}
                        ),
                        **(
                            {"parameter_enrichment": parameter_enrichment}
                            if parameter_enrichment is not None
                            else {}
                        ),
                    }
                )
            except (
                AIError,
                CLIError,
                ConfigError,
                DecisionError,
                ParameterAnalysisError,
                PDFDocumentError,
                StateError,
                WikiJSError,
                ValueError,
                TypeError,
            ) as exc:
                failures.append(
                    {
                        "product_id": candidate.product_id,
                        "path": candidate.wiki_path,
                        "error": _safe_error(exc),
                    }
                )

    home: dict[str, Any] | None = None
    home_skipped: str | None = None
    if args.refresh_home:
        if failures:
            home_skipped = "content_refresh_failures"
        else:
            home = publish_home()

    payload: dict[str, Any] = {
        "ok": not failures,
        "apply": True,
        "hydrate_parameters": bool(args.hydrate_parameters),
        "analyze_parameters": bool(args.analyze_parameters),
        "content_schema_version": CONTENT_SCHEMA_VERSION,
        "candidate_count": len(candidates),
        "refreshed_count": len(refreshed),
        "failed_count": len(failures),
        "refreshed": refreshed,
        "failures": failures,
    }
    if home is not None:
        payload["home"] = home["home"]
    if home_skipped is not None:
        payload["home_skipped"] = home_skipped
    _emit(payload)
    return 0 if not failures else 1


def publish_home() -> dict[str, Any]:
    """Create or update the reader-facing catalogue landing page."""

    settings = WikiSettings.from_env()
    with _store() as store:
        counts = store.status_counts()
        due_now = len(store.list_due(limit=1_000_000))
        published = store.published_products()

    managed = render_home_page(
        [
            _home_catalogue_product(item, path_prefix=settings.path_prefix)
            for item in published
        ],
        title=settings.home_title,
    )
    client = WikiJSClient(
        settings.base_url,
        settings.token,
        timeout=settings.timeout,
        new_page_private=settings.new_page_private,
        new_page_published=settings.new_page_published,
    )
    result = client.upsert_page(
        settings.home_path,
        settings.locale,
        settings.home_title,
        "按品牌、产品类别和最近更新浏览经过资料核验的产品百科。",
        managed,
        ["homepage", "managed-by-pv-wiki", "product-catalogue"],
    )
    page = result.get("page") if isinstance(result.get("page"), dict) else {}
    return {
        "ok": True,
        "home": {
            "action": result.get("action"),
            "id": page.get("id"),
            "path": settings.home_path,
            "locale": settings.locale,
        },
        "updated_products": len(published),
        "counts": counts,
        "due_now": due_now,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pv-wiki",
        description="Research one PostgreSQL product and safely maintain Wiki.js.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor", help="validate configuration and optional live reads")
    doctor.add_argument("--live", action="store_true", help="read PostgreSQL and Wiki.js")
    doctor.set_defaults(func=_cmd_doctor)

    sync_db = subparsers.add_parser("sync-db", help="read PostgreSQL and refresh the local queue")
    sync_db.set_defaults(func=_cmd_sync_db)

    precheck = subparsers.add_parser("precheck", help="report whether any product is due")
    precheck.set_defaults(func=_cmd_precheck)

    claim = subparsers.add_parser("claim", help="atomically claim one due product")
    claim.add_argument("--worker-id")
    claim.add_argument("--lease-seconds", type=int, default=3600)
    claim.set_defaults(func=_cmd_claim)

    search = subparsers.add_parser(
        "search",
        help="perform bounded searches with the configured provider",
    )
    search.add_argument("--lease-token", required=True)
    search.add_argument("--max-results", type=int, default=5)
    search.add_argument("--timeout", type=float, default=30.0)
    search.set_defaults(func=_cmd_search)

    extract = subparsers.add_parser(
        "extract",
        help="extract selected URLs through the configured provider",
    )
    extract.add_argument("--lease-token", required=True)
    extract.add_argument("--request-file", required=True)
    extract.add_argument("--timeout", type=float, default=30.0)
    extract.set_defaults(func=_cmd_extract)

    publish = subparsers.add_parser("publish", help="validate a decision and apply its outcome")
    publish.add_argument("--decision-file", required=True)
    publish.add_argument(
        "--evidence-file",
        help="provider extract JSON required when the decision outcome is publish",
    )
    publish.set_defaults(func=_cmd_publish)

    run_one_parser = subparsers.add_parser(
        "run-one",
        help="process one due product with web search, configured AI, and Wiki.js",
    )
    run_one_parser.add_argument("--worker-id")
    run_one_parser.add_argument("--lease-seconds", type=int, default=3600)
    run_one_parser.add_argument("--max-results", type=int, default=5)
    run_one_parser.add_argument(
        "--search-timeout",
        dest="search_timeout",
        type=float,
        default=30.0,
    )
    run_one_parser.set_defaults(func=_cmd_run_one)

    serve_parser = subparsers.add_parser(
        "serve",
        help="serve fixed authenticated operations for an internal n8n workflow",
    )
    serve_parser.add_argument("--host")
    serve_parser.add_argument("--port", type=int)
    serve_parser.set_defaults(func=_cmd_serve)

    fail = subparsers.add_parser("fail", help="record a bounded failure and release a lease")
    fail.add_argument("--lease-token", required=True)
    fail.add_argument("--reason", required=True)
    fail.set_defaults(func=_cmd_fail)

    status = subparsers.add_parser("status", help="show queue counts and recent product attempts")
    status.add_argument("--product-id")
    status.set_defaults(func=_cmd_status)

    requeue = subparsers.add_parser(
        "requeue",
        help="selectively wake finished outcomes after an operator policy change",
    )
    requeue.add_argument(
        "--outcome",
        action="append",
        required=True,
        help="last outcome to wake; repeat for multiple outcomes",
    )
    requeue.add_argument("--reason", required=True)
    requeue.add_argument("--attempted-after")
    requeue.add_argument("--attempted-before")
    requeue.add_argument("--limit", type=int, default=1000)
    requeue.set_defaults(func=_cmd_requeue)

    publish_home = subparsers.add_parser(
        "publish-home",
        help="create or update the managed Wiki.js landing page",
    )
    publish_home.set_defaults(func=_cmd_publish_home)

    refresh_content = subparsers.add_parser(
        "refresh-content",
        help=(
            "re-render existing pages from their last synced decision "
            "without search or AI"
        ),
    )
    refresh_content.add_argument(
        "--product-id",
        action="append",
        help="refresh only this exact product ID; repeat for multiple products",
    )
    refresh_content.add_argument("--limit", type=int, default=1000)
    refresh_content.add_argument(
        "--apply",
        action="store_true",
        help="apply existing-page-only Wiki.js updates; otherwise preview",
    )
    refresh_content.add_argument(
        "--hydrate-parameters",
        action="store_true",
        help=(
            "re-read each stored primary manufacturer PDF and rebuild its "
            "target-model parameter table without search or AI"
        ),
    )
    refresh_content.add_argument(
        "--analyze-parameters",
        action="store_true",
        help=(
            "translate every hydrated parameter and generate a complete-set, "
            "basis-linked professional analysis; requires --hydrate-parameters"
        ),
    )
    refresh_content.add_argument(
        "--refresh-home",
        action="store_true",
        help="refresh the homepage after a fully successful applied batch",
    )
    refresh_content.set_defaults(func=_cmd_refresh_content)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (
        CLIError,
        AIError,
        ConfigError,
        DecisionError,
        LeaseLostError,
        StateError,
        ExaError,
        WorkerConfigError,
        WikiJSError,
        ValueError,
    ) as exc:
        _emit(
            {
                "ok": False,
                "error_type": exc.__class__.__name__,
                "error": _safe_error(exc),
            },
            stream=sys.stderr,
        )
        return 2
    except Exception as exc:  # Last-resort JSON boundary for scheduler/CLI use.
        _emit(
            {
                "ok": False,
                "error_type": exc.__class__.__name__,
                "error": _safe_error(exc),
            },
            stream=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
