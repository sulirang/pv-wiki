"""Minimal durable completion state for Hermes-owned product research.

This module deliberately does not model attempts, leases, retries, research
rounds, queries, credits, deadlines, or provider actions.  Hermes owns the
research session and Exa access.  PV Wiki records only the durable boundary
that matters across fresh Hermes sessions:

* a product has a completed research decision; and
* a publishable decision has (or has not) been applied to Wiki.js.

The completion table and active catalogue snapshot live beside the legacy PV
Wiki state tables. On first open, the snapshot is backfilled once from legacy
products, while products with ``last_success_at`` are backfilled as already
completed and published. This prevents the new Hermes scheduler from
researching pages that the former worker already published.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .catalogue_snapshot import (
    CATALOGUE_TABLE,
    CatalogueSnapshotStatus,
    catalogue_snapshot_status,
    initialize_catalogue_snapshot,
)
from .config import (
    WikiSettings,
    allow_mirrors,
    min_fact_confidence,
    min_publish_confidence,
    public_brand_alias,
    redact_environment_secrets,
    state_target,
    trusted_source_domains_for_product,
)
from .decision import (
    _decision_identifies_generic_hardware,
    _quote_supports_fact,
    _quote_supports_out_of_scope,
    _structured_table_quote_supports_fact,
    _url_has_trusted_domain,
    canonical_product_category,
    identity_key,
    model_matches_catalogue_identity,
    preferred_catalogue_model,
    text_contains_catalogue_identity,
    text_contains_exact_identity,
)
from .evidence_receipt import (
    EvidenceReceiptError,
    content_sha256,
    resolve_evidence_hmac_key,
    verify_evidence_receipt,
)
from .render import (
    render_product_page,
    stable_path,
    stable_slug,
    validate_public_http_url,
)
from .state import StateStore
from .wikijs import WikiJSClient


COMPLETION_TABLE = "product_research_completions"
HERMES_DECISION_SCHEMA_VERSION = "2"
RESEARCH_OUTCOMES = frozenset(
    {
        "publish",
        "no_datasheet",
        "ambiguous",
        "insufficient_identity",
        "out_of_scope",
    }
)
SOURCE_TYPES = frozenset(
    {"manufacturer", "regulatory", "authorized", "mirror", "community"}
)
TRUSTED_SOURCE_TYPES = frozenset({"manufacturer", "regulatory", "authorized"})

_CREATE_COMPLETIONS = f"""
CREATE TABLE IF NOT EXISTS {COMPLETION_TABLE} (
    product_id TEXT PRIMARY KEY,
    source_hash TEXT NOT NULL,
    product_json TEXT NOT NULL,
    decision_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    outcome TEXT NOT NULL,
    publishable INTEGER NOT NULL CHECK (publishable IN (0, 1)),
    completed_at TEXT NOT NULL,
    published_at TEXT,
    wiki_path TEXT,
    wiki_action TEXT,
    CHECK (published_at IS NULL OR publishable = 1)
)
"""

_CREATE_PENDING_INDEX = f"""
CREATE INDEX IF NOT EXISTS product_research_completions_pending_idx
ON {COMPLETION_TABLE}(publishable, published_at, completed_at, product_id)
"""


class CompletionError(RuntimeError):
    """Base error for Hermes completion state."""


class CompletionValidationError(CompletionError, ValueError):
    """Raised when a proposed research result is unsafe to persist."""


class ProductNotFoundError(CompletionError):
    """Raised when a result names a product absent from local catalogue state."""


class ProductSourceChangedError(CompletionError):
    """Raised when Hermes researched an older catalogue snapshot."""


@dataclass(frozen=True, slots=True)
class ResearchProduct:
    product_id: str
    source_hash: str
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ResearchCompletion:
    product_id: str
    source_hash: str
    product: dict[str, Any]
    decision: dict[str, Any]
    evidence: list[dict[str, str]]
    outcome: str
    publishable: bool
    completed_at: datetime
    published_at: datetime | None
    wiki_path: str | None
    wiki_action: str | None


@dataclass(frozen=True, slots=True)
class SaveCompletionResult:
    completion: ResearchCompletion
    created: bool


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
        raise CompletionError("completion timestamp is invalid")
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError as exc:
        raise CompletionError("completion timestamp is invalid") from exc


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=_json_default,
    )


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return _time_text(value)
    raise TypeError(f"{type(value).__name__} is not JSON serializable")


def _json_object(value: Any, *, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise CompletionValidationError(f"{name} must be an object")
    return {str(key): item for key, item in value.items()}


def _json_array(value: Any, *, name: str) -> list[Any]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise CompletionValidationError(f"{name} must be an array")
    return list(value)


def _text(
    value: Any,
    *,
    name: str,
    required: bool = False,
    maximum: int = 4000,
) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str):
        raise CompletionValidationError(f"{name} must be a string")
    normalized = " ".join(value.split())
    if required and not normalized:
        raise CompletionValidationError(f"{name} is required")
    if len(normalized) > maximum:
        raise CompletionValidationError(f"{name} is too long")
    return normalized


def _public_url(value: Any, *, name: str) -> str:
    if not isinstance(value, str):
        raise CompletionValidationError(f"{name} must be a URL string")
    try:
        url = validate_public_http_url(value)
        port = urlsplit(url).port
    except ValueError as exc:
        raise CompletionValidationError(f"{name} is not a public HTTP(S) URL") from exc
    if port not in {None, 80, 443}:
        raise CompletionValidationError(f"{name} uses a non-standard port")
    return url


def _normalize_evidence_documents(
    evidence_documents: Any,
    *,
    evidence_hmac_key: bytes,
) -> tuple[list[dict[str, str]], dict[str, str]]:
    documents = _json_array(evidence_documents, name="evidence_documents")
    normalized: list[dict[str, str]] = []
    bodies: dict[str, str] = {}
    for index, raw in enumerate(documents):
        item = _json_object(raw, name=f"evidence_documents[{index}]")
        url = _public_url(item.get("url"), name=f"evidence_documents[{index}].url")
        content = item.get("content", item.get("raw_content"))
        if not isinstance(content, str) or not content.strip():
            raise CompletionValidationError(
                f"evidence_documents[{index}].content is required"
            )
        try:
            receipt_url = verify_evidence_receipt(
                evidence_hmac_key,
                url=url,
                content=content,
                receipt=item.get("receipt"),
            )
        except EvidenceReceiptError as exc:
            raise CompletionValidationError(
                f"evidence_documents[{index}].receipt is invalid"
            ) from exc
        if receipt_url != url:  # defensive: both use the same URL normalizer
            raise CompletionValidationError(
                f"evidence_documents[{index}].receipt URL is invalid"
            )
        previous = bodies.get(url)
        if previous is not None and previous != content:
            raise CompletionValidationError(
                "duplicate evidence URL has different content"
            )
        if previous is None:
            bodies[url] = content
            normalized.append(
                {
                    "url": url,
                    "sha256": content_sha256(content),
                }
            )
    normalized.sort(key=lambda item: item["url"].casefold())
    return normalized, bodies


def _normalize_source_items(
    value: Any,
    *,
    name: str,
    evidence_urls: set[str],
    datasheet: bool,
) -> list[dict[str, Any]]:
    items = _json_array(value, name=name)
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(items):
        item = _json_object(raw, name=f"{name}[{index}]")
        allowed_fields = {"url", "title", "source_type"}
        if datasheet:
            allowed_fields.add("is_primary")
        unknown = set(item) - allowed_fields
        if unknown:
            raise CompletionValidationError(
                f"{name}[{index}] has unknown fields: {', '.join(sorted(unknown))}"
            )
        url = _public_url(item.get("url"), name=f"{name}[{index}].url")
        if url not in evidence_urls:
            raise CompletionValidationError(
                f"{name}[{index}].url was not supplied as extracted evidence"
            )
        if url in seen:
            raise CompletionValidationError(f"{name} contains duplicate URLs")
        source_type = item.get("source_type")
        if source_type not in SOURCE_TYPES:
            raise CompletionValidationError(
                f"{name}[{index}].source_type is invalid"
            )
        result: dict[str, Any] = {
            "url": url,
            "title": _text(
                item.get("title"),
                name=f"{name}[{index}].title",
                required=True,
                maximum=500,
            ),
            "source_type": source_type,
        }
        if datasheet:
            is_primary = item.get("is_primary", False)
            if not isinstance(is_primary, bool):
                raise CompletionValidationError(
                    f"{name}[{index}].is_primary must be boolean"
                )
            result["is_primary"] = is_primary
        normalized.append(result)
        seen.add(url)
    return normalized


def _normalize_url_array(
    value: Any,
    *,
    name: str,
    allowed: set[str],
    require_nonempty: bool = False,
) -> list[str]:
    values = _json_array(value, name=name)
    result: list[str] = []
    seen: set[str] = set()
    for index, raw in enumerate(values):
        url = _public_url(raw, name=f"{name}[{index}]")
        if url not in allowed:
            raise CompletionValidationError(
                f"{name}[{index}] is outside the supplied source set"
            )
        if url not in seen:
            seen.add(url)
            result.append(url)
    if require_nonempty and not result:
        raise CompletionValidationError(f"{name} must cite at least one source")
    return result


def _normalize_facts(
    value: Any,
    *,
    source_urls: set[str],
    bodies: Mapping[str, str],
    expected_model: str,
) -> list[dict[str, Any]]:
    facts = _json_array(value, name="facts")
    normalized: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for index, raw in enumerate(facts):
        item = _json_object(raw, name=f"facts[{index}]")
        unknown = set(item) - {
            "name",
            "value",
            "unit",
            "category",
            "confidence",
            "evidence_urls",
            "evidence_quotes",
        }
        if unknown:
            raise CompletionValidationError(
                f"facts[{index}] has unknown fields: {', '.join(sorted(unknown))}"
            )
        name = _text(
            item.get("name"),
            name=f"facts[{index}].name",
            required=True,
            maximum=300,
        )
        name_key = identity_key(name)
        if not name_key or name_key in seen_names:
            raise CompletionValidationError("fact names must be non-empty and unique")
        seen_names.add(name_key)
        value_item = item.get("value")
        if value_item is None or isinstance(value_item, (Mapping, list, tuple)):
            raise CompletionValidationError(f"facts[{index}].value must be scalar")
        if isinstance(value_item, float) and not math.isfinite(value_item):
            raise CompletionValidationError(f"facts[{index}].value must be finite")
        citations = _normalize_url_array(
            item.get("evidence_urls"),
            name=f"facts[{index}].evidence_urls",
            allowed=source_urls,
            require_nonempty=True,
        )
        raw_quotes = item.get("evidence_quotes", [])
        quotes = _json_array(raw_quotes, name=f"facts[{index}].evidence_quotes")
        normalized_quotes: list[dict[str, str]] = []
        for quote_index, raw_quote in enumerate(quotes):
            quote_item = _json_object(
                raw_quote,
                name=f"facts[{index}].evidence_quotes[{quote_index}]",
            )
            quote_unknown = set(quote_item) - {"url", "quote", "model_quote"}
            if quote_unknown:
                raise CompletionValidationError(
                    f"facts[{index}].evidence_quotes[{quote_index}] has "
                    f"unknown fields: {', '.join(sorted(quote_unknown))}"
                )
            quote_url = _public_url(
                quote_item.get("url"),
                name=f"facts[{index}].evidence_quotes[{quote_index}].url",
            )
            if quote_url not in citations:
                raise CompletionValidationError(
                    f"facts[{index}] quote URL is not one of its citations"
                )
            raw_quote_value = quote_item.get("quote")
            if not isinstance(raw_quote_value, str):
                raise CompletionValidationError(
                    f"facts[{index}].evidence_quotes[{quote_index}].quote "
                    "must be a string"
                )
            # Preserve internal whitespace: PDF and table extracts commonly
            # place a newline inside the exact supporting span.
            quote = raw_quote_value.strip()
            if not quote:
                raise CompletionValidationError(
                    f"facts[{index}].evidence_quotes[{quote_index}].quote is required"
                )
            if len(quote) > 1000:
                raise CompletionValidationError(
                    f"facts[{index}].evidence_quotes[{quote_index}].quote is too long"
                )
            if quote not in bodies[quote_url]:
                raise CompletionValidationError(
                    f"facts[{index}] quote is not an exact evidence span"
                )
            model_quote_value = quote_item.get("model_quote")
            supports_fact = _quote_supports_fact(
                quote,
                name=name,
                value=value_item,
                unit=_text(
                    item.get("unit"),
                    name=f"facts[{index}].unit",
                    maximum=80,
                ),
                expected_product_name=expected_model,
                normalized_body=identity_key(bodies[quote_url]),
            )
            normalized_quote = {"url": quote_url, "quote": quote}
            if model_quote_value is not None:
                if not isinstance(model_quote_value, str):
                    raise CompletionValidationError(
                        f"facts[{index}].evidence_quotes[{quote_index}].model_quote "
                        "must be a string"
                    )
                model_quote = model_quote_value.strip()
                if not model_quote or len(model_quote) > 1000:
                    raise CompletionValidationError(
                        f"facts[{index}].evidence_quotes[{quote_index}].model_quote "
                        "is invalid"
                    )
                if model_quote not in bodies[quote_url]:
                    raise CompletionValidationError(
                        f"facts[{index}] model quote is not an exact evidence span"
                    )
                supports_fact = _structured_table_quote_supports_fact(
                    model_quote=model_quote,
                    fact_quote=quote,
                    name=name,
                    value=value_item,
                    unit=_text(
                        item.get("unit"),
                        name=f"facts[{index}].unit",
                        maximum=80,
                    ),
                    expected_product_name=expected_model,
                )
                normalized_quote["model_quote"] = model_quote
            if not supports_fact:
                raise CompletionValidationError(
                    f"facts[{index}] quote does not bind the exact model, "
                    "fact name, and value"
                )
            normalized_quotes.append(normalized_quote)
        if not normalized_quotes:
            raise CompletionValidationError(
                f"facts[{index}].evidence_quotes must include an exact evidence span"
            )
        result: dict[str, Any] = {
            "name": name,
            "value": value_item,
            "unit": _text(item.get("unit"), name=f"facts[{index}].unit", maximum=80),
            "category": _text(
                item.get("category"),
                name=f"facts[{index}].category",
                maximum=100,
            ),
            "evidence_urls": citations,
            "evidence_quotes": normalized_quotes,
        }
        confidence = item.get("confidence", 1.0)
        if (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not 0 <= float(confidence) <= 1
        ):
            raise CompletionValidationError(
                f"facts[{index}].confidence must be between 0 and 1"
            )
        if float(confidence) < min_fact_confidence():
            raise CompletionValidationError(
                f"facts[{index}].confidence is below the configured threshold"
            )
        result["confidence"] = float(confidence)
        normalized.append(result)
    return normalized


def _normalize_conclusion_evidence(
    value: Any,
    *,
    outcome: str,
    product_id: str,
    product_name: str,
    bodies: Mapping[str, str],
) -> list[dict[str, str]]:
    """Ground a permanent non-publish conclusion in extracted source text."""

    items = _json_array(value, name="conclusion_evidence")
    normalized: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for index, raw in enumerate(items):
        item = _json_object(raw, name=f"conclusion_evidence[{index}]")
        unknown = set(item) - {"url", "quote"}
        if unknown:
            raise CompletionValidationError(
                f"conclusion_evidence[{index}] has unknown fields: "
                f"{', '.join(sorted(unknown))}"
            )
        url = _public_url(
            item.get("url"),
            name=f"conclusion_evidence[{index}].url",
        )
        if url not in bodies:
            raise CompletionValidationError(
                f"conclusion_evidence[{index}].url was not supplied as "
                "extracted evidence"
            )
        raw_quote = item.get("quote")
        if not isinstance(raw_quote, str):
            raise CompletionValidationError(
                f"conclusion_evidence[{index}].quote must be a string"
            )
        quote = raw_quote.strip()
        if not quote or len(quote) > 1000:
            raise CompletionValidationError(
                f"conclusion_evidence[{index}].quote is invalid"
            )
        if quote not in bodies[url]:
            raise CompletionValidationError(
                f"conclusion_evidence[{index}].quote is not an exact "
                "evidence span"
            )
        key = (url, quote)
        if key not in seen:
            seen.add(key)
            normalized.append({"url": url, "quote": quote})
    if not normalized:
        raise CompletionValidationError(
            "non-publish decisions require conclusion_evidence"
        )

    if outcome in {"no_datasheet", "ambiguous", "insufficient_identity"} and not any(
        text_contains_catalogue_identity(product_id, product_name, item["quote"])
        for item in normalized
    ):
        raise CompletionValidationError(
            f"{outcome} conclusion evidence must identify the catalogue product"
        )

    if outcome == "out_of_scope":
        decision_identity = preferred_catalogue_model(
            product_id,
            product_name,
            allow_product_id=True,
        )
        if not decision_identity or not any(
            _quote_supports_out_of_scope(
                item["quote"],
                expected_product_name=decision_identity,
                body=bodies[item["url"]],
            )
            for item in normalized
        ):
            raise CompletionValidationError(
                "out_of_scope requires an exact target-model hardware-type quote"
            )
    return normalized


def _validate_publish_source_policy(
    *,
    product: Mapping[str, Any],
    manufacturer: str,
    model: str,
    product_category: str,
    summary: str,
    datasheets: list[dict[str, Any]],
    sources: list[dict[str, Any]],
    facts: list[dict[str, Any]],
    review_summary: str,
    review_evidence_urls: list[str],
    bodies: Mapping[str, str],
) -> None:
    """Apply the legacy publication authority model without research caps."""

    brand_code = str(product.get("brand_code") or "").strip()
    product_name = str(product.get("product_name") or "")
    operator_manufacturer = public_brand_alias(brand_code)
    if not operator_manufacturer:
        raise CompletionValidationError(
            "publish requires an operator-approved catalogue brand identity"
        )
    if identity_key(manufacturer) != identity_key(operator_manufacturer):
        raise CompletionValidationError(
            "publish manufacturer conflicts with the operator-approved "
            "catalogue brand identity"
        )
    if not model_matches_catalogue_identity(
        model,
        product_id=str(product.get("product_id") or ""),
        product_name=product_name,
    ):
        raise CompletionValidationError(
            "publish model is not bound to the catalogue identity"
        )
    if any(
        not text_contains_exact_identity(model, bodies[item["url"]])
        for item in [*datasheets, *sources]
    ):
        raise CompletionValidationError(
            "cited publish evidence must contain the catalogue-bound model identity"
        )
    if _decision_identifies_generic_hardware(
        model=model,
        product_category=product_category,
        summary=summary,
    ):
        raise CompletionValidationError(
            "publish cannot classify the catalogue product as generic hardware"
        )

    trusted_domains = trusted_source_domains_for_product(
        brand_code,
        manufacturer,
    )
    untrusted_authority_urls = {
        item["url"]
        for item in [*datasheets, *sources]
        if item["source_type"] in TRUSTED_SOURCE_TYPES
        and not _url_has_trusted_domain(item["url"], trusted_domains)
    }
    if untrusted_authority_urls:
        raise CompletionValidationError(
            "manufacturer source URLs and regulatory or authorized source URLs "
            "must belong to an operator-configured trusted domain"
        )

    if any(item["source_type"] == "community" for item in datasheets):
        raise CompletionValidationError("community sources cannot be datasheets")
    source_types_by_url = {
        item["url"]: item["source_type"] for item in [*datasheets, *sources]
    }
    community_urls = {
        url
        for url, source_type in source_types_by_url.items()
        if source_type == "community"
    }
    if review_summary and len(set(review_evidence_urls)) < 2:
        raise CompletionValidationError(
            "review_summary requires at least two review evidence URLs"
        )
    if review_evidence_urls and not review_summary:
        raise CompletionValidationError(
            "review_evidence_urls require review_summary"
        )
    if not community_urls <= set(review_evidence_urls):
        raise CompletionValidationError(
            "community sources may be cited only as review evidence"
        )

    if len(facts) < 5:
        raise CompletionValidationError(
            "publish requires at least 5 cited specification facts"
        )
    primary = [item for item in datasheets if item["is_primary"]]
    if not primary:
        raise CompletionValidationError("publish requires a primary datasheet")
    configured_primary = [
        item
        for item in primary
        if item["source_type"] in TRUSTED_SOURCE_TYPES
        and _url_has_trusted_domain(item["url"], trusted_domains)
    ]
    if not configured_primary:
        mirror_domains = {
            ".".join((urlsplit(item["url"]).hostname or "").split(".")[-2:])
            for item in primary
            if item["source_type"] == "mirror"
        }
        if (
            not allow_mirrors()
            or len(primary) < 2
            or len(mirror_domains) < 2
            or any(item["source_type"] != "mirror" for item in primary)
        ):
            raise CompletionValidationError(
                "primary datasheet needs an operator-configured trusted domain "
                "or two explicitly enabled independent mirrors"
            )

    configured_primary_urls = {item["url"] for item in configured_primary}
    for index, fact in enumerate(facts):
        if set(fact["evidence_urls"]) & community_urls:
            raise CompletionValidationError(
                f"facts[{index}] cannot use community review evidence"
            )
        quoted_urls = {item["url"] for item in fact["evidence_quotes"]}
        if configured_primary_urls:
            trusted = bool(quoted_urls & configured_primary_urls)
        else:
            trusted = len(
                {
                    ".".join((urlsplit(url).hostname or "").split(".")[-2:])
                    for url in quoted_urls
                    if source_types_by_url.get(url) == "mirror"
                }
            ) >= 2
        if not trusted:
            raise CompletionValidationError(
                f"facts[{index}] needs supporting quotes from the trusted "
                "primary datasheet"
            )


def validate_research_decision(
    raw_decision: Any,
    *,
    product_id: str,
    product: Mapping[str, Any],
    evidence_documents: Any,
    evidence_hmac_key: bytes,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Validate a Hermes decision and return storage-safe normalized data.

    This is intentionally an evidence-integrity boundary, not a research
    budget. It has no limit on search rounds, query count, fetched URL count,
    credits, or elapsed time. It does preserve the former worker's publication
    safety boundary: catalogue identity, manufacturer authority, primary-source
    provenance, and fact-level grounding are checked before completion becomes
    permanent.
    """

    decision = _json_object(raw_decision, name="decision")
    allowed_decision_fields = {
        "schema_version",
        "product_id",
        "outcome",
        "confidence",
        "manufacturer",
        "model",
        "product_category",
        "summary",
        "review_summary",
        "review_evidence_urls",
        "decision_notes",
        "datasheets",
        "sources",
        "facts",
        "conflicts",
        "conclusion_evidence",
    }
    unknown = set(decision) - allowed_decision_fields
    if unknown:
        raise CompletionValidationError(
            f"decision has unknown fields: {', '.join(sorted(unknown))}"
        )
    if decision.get("schema_version") != HERMES_DECISION_SCHEMA_VERSION:
        raise CompletionValidationError(
            f"decision.schema_version must be {HERMES_DECISION_SCHEMA_VERSION!r}"
        )
    if "lease_token" in decision:
        raise CompletionValidationError("Hermes decisions must not contain lease_token")
    supplied_product_id = _text(
        decision.get("product_id"),
        name="decision.product_id",
        required=True,
        maximum=200,
    )
    if supplied_product_id != product_id:
        raise CompletionValidationError("decision.product_id does not match product")
    outcome = decision.get("outcome")
    if outcome not in RESEARCH_OUTCOMES:
        raise CompletionValidationError("decision.outcome is invalid")

    evidence, bodies = _normalize_evidence_documents(
        evidence_documents,
        evidence_hmac_key=evidence_hmac_key,
    )
    evidence_urls = set(bodies)
    publishable = outcome == "publish"
    if not evidence_urls:
        raise CompletionValidationError(
            "completed research decisions require extracted evidence"
        )

    raw_category = _text(
        decision.get("product_category"),
        name="decision.product_category",
        required=publishable or outcome == "out_of_scope",
        maximum=100,
    )
    normalized: dict[str, Any] = {
        "schema_version": HERMES_DECISION_SCHEMA_VERSION,
        "product_id": product_id,
        "outcome": outcome,
        "confidence": decision.get("confidence", 1.0),
        "manufacturer": _text(
            decision.get("manufacturer"),
            name="decision.manufacturer",
            required=publishable,
            maximum=300,
        ),
        "model": _text(
            decision.get("model"),
            name="decision.model",
            required=publishable,
            maximum=300,
        ),
        "product_category": canonical_product_category(raw_category),
        "summary": _text(
            decision.get("summary"),
            name="decision.summary",
            required=publishable or outcome == "out_of_scope",
            maximum=2000,
        ),
        "review_summary": _text(
            decision.get("review_summary"),
            name="decision.review_summary",
            maximum=1500,
        ),
        "decision_notes": _text(
            decision.get("decision_notes"),
            name="decision.decision_notes",
            required=True,
            maximum=2000,
        ),
    }
    confidence = normalized["confidence"]
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not 0 <= float(confidence) <= 1
    ):
        raise CompletionValidationError("decision.confidence must be between 0 and 1")
    normalized["confidence"] = float(confidence)
    if publishable and normalized["confidence"] < min_publish_confidence():
        raise CompletionValidationError(
            "publish confidence is below the configured threshold"
        )
    if outcome == "out_of_scope" and normalized["confidence"] < min_publish_confidence():
        raise CompletionValidationError(
            "out_of_scope confidence is below the configured threshold"
        )

    if publishable:
        datasheets = _normalize_source_items(
            decision.get("datasheets", []),
            name="datasheets",
            evidence_urls=evidence_urls,
            datasheet=True,
        )
        sources = _normalize_source_items(
            decision.get("sources", []),
            name="sources",
            evidence_urls=evidence_urls,
            datasheet=False,
        )
        declared_urls = {
            item["url"] for item in [*datasheets, *sources]
        }
        if len(declared_urls) != len(datasheets) + len(sources):
            raise CompletionValidationError(
                "a source URL cannot be declared more than once"
            )
        if not declared_urls:
            raise CompletionValidationError(
                "publish decisions require at least one declared source"
            )
        facts = _normalize_facts(
            decision.get("facts", []),
            source_urls=declared_urls,
            bodies=bodies,
            expected_model=normalized["model"],
        )
        normalized["datasheets"] = datasheets
        normalized["sources"] = sources
        normalized["facts"] = facts
        normalized["review_evidence_urls"] = _normalize_url_array(
            decision.get("review_evidence_urls", []),
            name="review_evidence_urls",
            allowed=declared_urls,
        )
        if decision.get("conclusion_evidence") not in (None, []):
            raise CompletionValidationError(
                "publish decisions must not contain conclusion_evidence"
            )
        _validate_publish_source_policy(
            product=product,
            manufacturer=normalized["manufacturer"],
            model=normalized["model"],
            product_category=normalized["product_category"],
            summary=normalized["summary"],
            datasheets=datasheets,
            sources=sources,
            facts=facts,
            review_summary=normalized["review_summary"],
            review_evidence_urls=normalized["review_evidence_urls"],
            bodies=bodies,
        )
        normalized["conclusion_evidence"] = []
    else:
        for field in (
            "datasheets",
            "sources",
            "facts",
            "review_evidence_urls",
        ):
            if decision.get(field) not in (None, []):
                raise CompletionValidationError(
                    f"{outcome} must not contain {field}"
                )
        if normalized["review_summary"]:
            raise CompletionValidationError(
                f"{outcome} must not contain review_summary"
            )
        normalized["datasheets"] = []
        normalized["sources"] = []
        normalized["facts"] = []
        normalized["review_evidence_urls"] = []
        normalized["review_summary"] = ""
        normalized["conclusion_evidence"] = _normalize_conclusion_evidence(
            decision.get("conclusion_evidence", []),
            outcome=outcome,
            product_id=product_id,
            product_name=str(product.get("product_name") or ""),
            bodies=bodies,
        )

    raw_conflicts = _json_array(decision.get("conflicts", []), name="conflicts")
    normalized_conflicts: list[dict[str, Any]] = []
    allowed_conflict_urls = set(bodies)
    for index, raw_conflict in enumerate(raw_conflicts):
        conflict = _json_object(raw_conflict, name=f"conflicts[{index}]")
        unknown = set(conflict) - {"field", "values", "source_urls"}
        if unknown:
            raise CompletionValidationError(
                f"conflicts[{index}] has unknown fields: "
                f"{', '.join(sorted(unknown))}"
            )
        urls = _normalize_url_array(
            conflict.get("source_urls", []),
            name=f"conflicts[{index}].source_urls",
            allowed=allowed_conflict_urls,
            require_nonempty=True,
        )
        values = _json_array(
            conflict.get("values", []),
            name=f"conflicts[{index}].values",
        )
        if len(values) < 2 or any(
            value is None or isinstance(value, (Mapping, list, tuple))
            for value in values
        ):
            raise CompletionValidationError(
                f"conflicts[{index}].values needs at least two scalar values"
            )
        if any(isinstance(value, float) and not math.isfinite(value) for value in values):
            raise CompletionValidationError(
                f"conflicts[{index}].values must be finite"
            )
        normalized_conflicts.append(
            {
                "field": _text(
                    conflict.get("field"),
                    name=f"conflicts[{index}].field",
                    required=True,
                    maximum=300,
                ),
                "values": values,
                "source_urls": urls,
            }
        )
    if (
        outcome in {"no_datasheet", "insufficient_identity", "out_of_scope"}
        and normalized_conflicts
    ):
        raise CompletionValidationError(
            f"{outcome} must not contain verified conflicts"
        )
    fact_names = {identity_key(item["name"]) for item in normalized["facts"]}
    conflict_fields = {
        identity_key(item["field"]) for item in normalized_conflicts
    }
    if fact_names & conflict_fields:
        raise CompletionValidationError(
            "conflicted fields must not also appear as verified facts"
        )
    normalized["conflicts"] = normalized_conflicts
    return normalized, evidence


def _decision_urls(decision: Mapping[str, Any]) -> list[dict[str, str]]:
    urls: set[str] = set()
    for key in ("datasheets", "sources"):
        items = decision.get(key, [])
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, Mapping) or not isinstance(item.get("url"), str):
                continue
            try:
                urls.add(validate_public_http_url(item["url"]))
            except ValueError:
                continue
    return [{"url": url, "sha256": ""} for url in sorted(urls, key=str.casefold)]


class HermesCompletionStore:
    """Completion registry sharing the existing PV Wiki state database."""

    def __init__(
        self,
        target: str | Path | None = None,
        *,
        timeout: float = 5.0,
        evidence_hmac_key: bytes | bytearray | memoryview | None = None,
    ) -> None:
        self._evidence_hmac_key = resolve_evidence_hmac_key(evidence_hmac_key)
        self._state = StateStore(target if target is not None else state_target(), timeout=timeout)
        self.backend = self._state.backend
        self.location = self._state.location
        initialize_catalogue_snapshot(self._state)
        self._initialize()

    def close(self) -> None:
        self._state.close()

    def __enter__(self) -> "HermesCompletionStore":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    def _initialize(self) -> None:
        with self._state._write_transaction() as connection:
            connection.execute(_CREATE_COMPLETIONS)
            connection.execute(_CREATE_PENDING_INDEX)
            self._backfill_legacy_publications(connection)

    def _backfill_legacy_publications(self, connection: Any) -> None:
        rows = connection.execute(
            """
            SELECT
                p.product_id,
                p.source_hash,
                p.payload_json,
                p.last_success_at,
                a.details_json
            FROM products AS p
            LEFT JOIN attempts AS a
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
                  FROM product_research_completions AS completed
                  WHERE completed.product_id = p.product_id
              )
            ORDER BY p.product_id
            """
        ).fetchall()
        for row in rows:
            product = self._decoded_object(row["payload_json"])
            details = self._decoded_object(row["details_json"])
            recorded_payload = details.get("payload")
            decision = (
                recorded_payload.get("decision")
                if isinstance(recorded_payload, Mapping)
                and isinstance(recorded_payload.get("decision"), Mapping)
                else {}
            )
            wiki_path = details.get("wiki_path")
            if not isinstance(wiki_path, str) or not wiki_path.strip():
                wiki_path = None
            wiki_action = (
                recorded_payload.get("wiki_action")
                if isinstance(recorded_payload, Mapping)
                and isinstance(recorded_payload.get("wiki_action"), str)
                else "legacy"
            )
            connection.execute(
                f"""
                INSERT INTO {COMPLETION_TABLE} (
                    product_id, source_hash, product_json, decision_json,
                    evidence_json, outcome, publishable, completed_at,
                    published_at, wiki_path, wiki_action
                ) VALUES (?, ?, ?, ?, ?, 'publish', 1, ?, ?, ?, ?)
                ON CONFLICT (product_id) DO NOTHING
                """,
                (
                    row["product_id"],
                    row["source_hash"],
                    _canonical_json(product),
                    _canonical_json(dict(decision)),
                    _canonical_json(_decision_urls(decision)),
                    row["last_success_at"],
                    row["last_success_at"],
                    wiki_path,
                    wiki_action,
                ),
            )

    @staticmethod
    def _decoded_object(value: Any) -> dict[str, Any]:
        if not isinstance(value, str):
            return {}
        try:
            decoded = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return {}
        return dict(decoded) if isinstance(decoded, Mapping) else {}

    def next_product(
        self,
        *,
        after_product_id: str | None = None,
    ) -> ResearchProduct | None:
        """Return the next product without a completion row.

        This is deliberately a read-only anti-join, not a claim or lease.  A
        caller may provide a process-local cursor for fair wraparound without
        creating durable scheduling or retry state. A single serialized Hermes
        cron job is the runtime ownership boundary.
        """

        cursor = None
        if after_product_id is not None:
            cursor = _text(
                after_product_id,
                name="after_product_id",
                required=True,
                maximum=200,
            )

        def select(connection: Any, after: str | None) -> Any:
            cursor_clause = "" if after is None else "AND p.product_id > ?"
            parameters: tuple[str, ...] = () if after is None else (after,)
            return connection.execute(
                f"""
                SELECT p.product_id, p.source_hash, p.payload_json
                FROM {CATALOGUE_TABLE} AS p
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM {COMPLETION_TABLE} AS completed
                    WHERE completed.product_id = p.product_id
                )
                {cursor_clause}
                ORDER BY p.product_id
                LIMIT 1
                """,
                parameters,
            ).fetchone()

        with self._state._connection() as connection:
            row = select(connection, cursor)
            if row is None and cursor is not None:
                row = select(connection, None)
        if row is None:
            return None
        return ResearchProduct(
            product_id=row["product_id"],
            source_hash=row["source_hash"],
            payload=self._decoded_object(row["payload_json"]),
        )

    def catalogue_status(self) -> CatalogueSnapshotStatus:
        """Return non-secret metadata for the active local catalogue snapshot."""

        return catalogue_snapshot_status(self._state)

    def get_completion(self, product_id: Any) -> ResearchCompletion | None:
        with self._state._connection() as connection:
            row = connection.execute(
                f"SELECT * FROM {COMPLETION_TABLE} WHERE product_id = ?",
                (str(product_id),),
            ).fetchone()
        return self._completion(row) if row is not None else None

    def pending_publication(self) -> ResearchCompletion | None:
        with self._state._connection() as connection:
            row = connection.execute(
                f"""
                SELECT *
                FROM {COMPLETION_TABLE}
                WHERE publishable = 1 AND published_at IS NULL
                ORDER BY completed_at, product_id
                LIMIT 1
                """
            ).fetchone()
        return self._completion(row) if row is not None else None

    def save_completion(
        self,
        *,
        product_id: str,
        source_hash: str,
        decision: Any,
        evidence_documents: Any,
        now: datetime | None = None,
    ) -> SaveCompletionResult:
        """Validate and atomically persist one completed research result.

        The product key is immutable.  A replay uses ``ON CONFLICT DO
        NOTHING`` and returns the existing row without overwriting it.
        """

        normalized_product_id = _text(
            product_id,
            name="product_id",
            required=True,
            maximum=200,
        )
        normalized_hash = _text(
            source_hash,
            name="source_hash",
            required=True,
            maximum=200,
        )
        # Verify the Exa-gateway provenance boundary before catalogue source,
        # identity, or fact checks, and before an idempotent replay is accepted.
        _normalize_evidence_documents(
            evidence_documents,
            evidence_hmac_key=self._evidence_hmac_key,
        )
        with self._state._connection() as connection:
            existing_row = connection.execute(
                f"SELECT * FROM {COMPLETION_TABLE} WHERE product_id = ?",
                (normalized_product_id,),
            ).fetchone()
            product_row = connection.execute(
                f"""
                SELECT product_id, source_hash, payload_json
                FROM {CATALOGUE_TABLE}
                WHERE product_id = ?
                """,
                (normalized_product_id,),
            ).fetchone()
        if existing_row is not None:
            return SaveCompletionResult(self._completion(existing_row), False)
        if product_row is None:
            raise ProductNotFoundError("product is absent from catalogue state")
        if product_row["source_hash"] != normalized_hash:
            raise ProductSourceChangedError(
                "product source changed after Hermes selected it"
            )
        product_payload = self._decoded_object(product_row["payload_json"])
        product_payload["product_id"] = normalized_product_id

        normalized_decision, evidence = validate_research_decision(
            decision,
            product_id=normalized_product_id,
            product=product_payload,
            evidence_documents=evidence_documents,
            evidence_hmac_key=self._evidence_hmac_key,
        )
        completed_at = _time_text(_utc(now))
        with self._state._write_transaction() as connection:
            product_row = connection.execute(
                f"""
                SELECT product_id, source_hash, payload_json
                FROM {CATALOGUE_TABLE}
                WHERE product_id = ?
                """,
                (normalized_product_id,),
            ).fetchone()
            if product_row is None:
                raise ProductNotFoundError("product is absent from catalogue state")
            if product_row["source_hash"] != normalized_hash:
                raise ProductSourceChangedError(
                    "product source changed after Hermes selected it"
                )
            cursor = connection.execute(
                f"""
                INSERT INTO {COMPLETION_TABLE} (
                    product_id, source_hash, product_json, decision_json,
                    evidence_json, outcome, publishable, completed_at,
                    published_at, wiki_path, wiki_action
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL)
                ON CONFLICT (product_id) DO NOTHING
                """,
                (
                    normalized_product_id,
                    normalized_hash,
                    product_row["payload_json"],
                    _canonical_json(normalized_decision),
                    _canonical_json(evidence),
                    normalized_decision["outcome"],
                    1 if normalized_decision["outcome"] == "publish" else 0,
                    completed_at,
                ),
            )
            created = cursor.rowcount == 1
            row = connection.execute(
                f"SELECT * FROM {COMPLETION_TABLE} WHERE product_id = ?",
                (normalized_product_id,),
            ).fetchone()
        if row is None:  # defensive: INSERT winner or existing row must exist
            raise CompletionError("completion insert did not produce a row")
        return SaveCompletionResult(self._completion(row), created)

    def mark_published(
        self,
        product_id: Any,
        *,
        wiki_path: str,
        wiki_action: str,
        now: datetime | None = None,
    ) -> ResearchCompletion:
        normalized_product_id = str(product_id)
        path = _text(wiki_path, name="wiki_path", required=True, maximum=1000)
        action = _text(wiki_action, name="wiki_action", required=True, maximum=100)
        with self._state._write_transaction() as connection:
            row = connection.execute(
                f"SELECT * FROM {COMPLETION_TABLE} WHERE product_id = ?",
                (normalized_product_id,),
            ).fetchone()
            if row is None:
                raise ProductNotFoundError("completion does not exist")
            if not bool(row["publishable"]):
                raise CompletionError("non-publishable completion cannot be published")
            if row["published_at"] is None:
                connection.execute(
                    f"""
                    UPDATE {COMPLETION_TABLE}
                    SET published_at = ?, wiki_path = ?, wiki_action = ?
                    WHERE product_id = ? AND published_at IS NULL
                    """,
                    (
                        _time_text(_utc(now)),
                        path,
                        action,
                        normalized_product_id,
                    ),
                )
            updated = connection.execute(
                f"SELECT * FROM {COMPLETION_TABLE} WHERE product_id = ?",
                (normalized_product_id,),
            ).fetchone()
        if updated is None:
            raise CompletionError("published completion disappeared")
        return self._completion(updated)

    def counts(self) -> dict[str, int]:
        with self._state._connection() as connection:
            row = connection.execute(
                f"""
                SELECT
                    COUNT(*) AS completed,
                    SUM(CASE WHEN publishable = 1 THEN 1 ELSE 0 END) AS publishable,
                    SUM(CASE WHEN published_at IS NOT NULL THEN 1 ELSE 0 END) AS published,
                    SUM(
                        CASE
                            WHEN publishable = 1 AND published_at IS NULL THEN 1
                            ELSE 0
                        END
                    ) AS pending_publication
                FROM {COMPLETION_TABLE}
                """
            ).fetchone()
        return {
            key: int(row[key] or 0)
            for key in (
                "completed",
                "publishable",
                "published",
                "pending_publication",
            )
        }

    @staticmethod
    def _completion(row: Any) -> ResearchCompletion:
        product_value = json.loads(row["product_json"])
        decision_value = json.loads(row["decision_json"])
        evidence_value = json.loads(row["evidence_json"])
        if not isinstance(product_value, Mapping):
            product_value = {}
        if not isinstance(decision_value, Mapping):
            decision_value = {}
        if not isinstance(evidence_value, list):
            evidence_value = []
        completed_at = _parse_time(row["completed_at"])
        if completed_at is None:  # excluded by the table constraint
            raise CompletionError("completion timestamp is missing")
        return ResearchCompletion(
            product_id=row["product_id"],
            source_hash=row["source_hash"],
            product=dict(product_value),
            decision=dict(decision_value),
            evidence=[
                dict(item) for item in evidence_value if isinstance(item, Mapping)
            ],
            outcome=row["outcome"],
            publishable=bool(row["publishable"]),
            completed_at=completed_at,
            published_at=_parse_time(row["published_at"]),
            wiki_path=row["wiki_path"],
            wiki_action=row["wiki_action"],
        )


def _tag_slug(prefix: str, value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return f"{prefix}-{stable_slug(text, max_length=64)}"
    except ValueError:
        return None


def _wiki_tags(product: Mapping[str, Any], decision: Mapping[str, Any]) -> list[str]:
    tags = {"product", "datasheet-found", "managed-by-pv-wiki"}
    for key in ("datasheets", "sources"):
        items = decision.get(key, [])
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, Mapping) and isinstance(item.get("source_type"), str):
                tags.add(f"source-{item['source_type']}")
    brand = _tag_slug(
        "brand", decision.get("manufacturer") or product.get("brand_code")
    )
    category = _tag_slug("category", decision.get("product_category"))
    if brand:
        tags.add(brand)
    if category:
        tags.add(category)
    return sorted(tags)


def publish_researched(
    store: HermesCompletionStore,
    product_id: str | None = None,
    *,
    settings: WikiSettings | None = None,
    client_factory: Any = WikiJSClient,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Publish one completed result without invoking research.

    Wiki.js is called before the durable published marker is written.  A
    failure therefore leaves the completion pending for a publish-only retry;
    it never puts the product back into the research anti-join.
    """

    completion = (
        store.get_completion(product_id)
        if product_id is not None
        else store.pending_publication()
    )
    if completion is None:
        return {
            "ok": True,
            "found": False,
            "published": False,
            "reason": "completion_not_found" if product_id else "no_pending_publication",
        }
    if not completion.publishable:
        return {
            "ok": True,
            "found": True,
            "product_id": completion.product_id,
            "outcome": completion.outcome,
            "published": False,
            "reason": "not_publishable",
        }
    if completion.published_at is not None:
        return {
            "ok": True,
            "found": True,
            "product_id": completion.product_id,
            "published": True,
            "changed": False,
            "wiki": {
                "action": completion.wiki_action,
                "path": completion.wiki_path,
            },
        }

    resolved_settings = settings or WikiSettings.from_env()
    product = {
        **completion.product,
        "manufacturer": completion.decision.get("manufacturer")
        or completion.product.get("brand_code"),
        "model": completion.decision.get("model")
        or completion.product.get("product_name"),
    }
    path = stable_path(product, prefix=resolved_settings.path_prefix)
    title = " ".join(
        str(
            completion.decision.get("model")
            or completion.product.get("product_name")
            or completion.product_id
        ).split()
    )
    summary = " ".join(str(completion.decision.get("summary") or "").split())
    description = (summary or f"Datasheet and cited specifications for {title}.")[
        :500
    ]
    managed = render_product_page(
        product,
        completion.decision,
        checked_at=completion.completed_at,
    )
    tags = _wiki_tags(completion.product, completion.decision)
    client = client_factory(
        resolved_settings.base_url,
        resolved_settings.token,
        timeout=resolved_settings.timeout,
        new_page_private=resolved_settings.new_page_private,
        new_page_published=resolved_settings.new_page_published,
    )
    result = client.upsert_page(
        path,
        resolved_settings.locale,
        title,
        description,
        managed,
        tags,
    )
    action = str(result.get("action") or "upserted")
    published = store.mark_published(
        completion.product_id,
        wiki_path=path,
        wiki_action=action,
        now=now,
    )
    page = result.get("page") if isinstance(result.get("page"), Mapping) else {}
    return {
        "ok": True,
        "found": True,
        "product_id": published.product_id,
        "published": True,
        "changed": action != "unchanged",
        "wiki": {
            "action": action,
            "id": page.get("id"),
            "path": path,
            "locale": resolved_settings.locale,
        },
    }


def _safe_cli_error(exc: BaseException) -> str:
    message = redact_environment_secrets(
        " ".join(str(exc).split()),
        ("WIKIJS_TOKEN", "PV_WIKI_STATE_DATABASE_URL", "PGPASSWORD"),
        limit=1000,
    )
    return (message or type(exc).__name__)[:1000]


def main_publish(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pv-wiki-publish-researched",
        description="Publish one completed Hermes research result to Wiki.js.",
    )
    parser.add_argument(
        "--product-id",
        help="publish this completion instead of the oldest pending completion",
    )
    args = parser.parse_args(argv)
    try:
        with HermesCompletionStore() as store:
            result = publish_researched(store, args.product_id)
    except Exception as exc:
        print(
            json.dumps(
                {"ok": False, "error": _safe_cli_error(exc)},
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, default=_json_default))
    return 0


__all__ = [
    "COMPLETION_TABLE",
    "CompletionError",
    "CompletionValidationError",
    "HERMES_DECISION_SCHEMA_VERSION",
    "HermesCompletionStore",
    "ProductNotFoundError",
    "ProductSourceChangedError",
    "ResearchCompletion",
    "ResearchProduct",
    "SaveCompletionResult",
    "main_publish",
    "publish_researched",
    "validate_research_decision",
]
