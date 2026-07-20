"""Validate the bounded AI-to-runtime decision contract."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from .render import validate_public_http_url


OUTCOMES = frozenset(
    {"publish", "no_datasheet", "ambiguous", "insufficient_identity"}
)
SOURCE_TYPES = frozenset(
    {"manufacturer", "regulatory", "authorized", "mirror", "community"}
)
TRUSTED_TYPES = frozenset({"manufacturer", "regulatory", "authorized"})
_PRODUCT_CATEGORY_ALIASES = {
    "heat pump": "热泵",
    "heat pumps": "热泵",
    "air source heat pump": "热泵",
    "air-to-water heat pump": "热泵",
    "ground source heat pump": "热泵",
    "water source heat pump": "热泵",
    "空气源热泵": "热泵",
    "空气能热泵": "热泵",
    "地源热泵": "热泵",
    "水源热泵": "热泵",
    "solar inverter": "光伏逆变器",
    "photovoltaic inverter": "光伏逆变器",
    "pv inverter": "光伏逆变器",
    "太阳能逆变器": "光伏逆变器",
}
_TOP_LEVEL = frozenset(
    {
        "schema_version",
        "product_id",
        "lease_token",
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
    }
)


class DecisionError(ValueError):
    """Raised when an agent decision is incomplete or unsafe to apply."""


def canonical_product_category(value: str) -> str:
    """Normalize common source-language aliases into broad reader categories."""

    cleaned = " ".join(value.split())
    return _PRODUCT_CATEGORY_ALIASES.get(cleaned.casefold(), cleaned)


def _text(value: Any, field: str, *, required: bool = False, limit: int = 2000) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str):
        raise DecisionError(f"{field} must be a string")
    value = value.strip()
    if required and not value:
        raise DecisionError(f"{field} is required")
    if len(value) > limit:
        raise DecisionError(f"{field} exceeds {limit} characters")
    return value


def _confidence(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DecisionError(f"{field} must be a number")
    number = float(value)
    if not 0.0 <= number <= 1.0:
        raise DecisionError(f"{field} must be between 0 and 1")
    return number


def validate_public_url(value: Any, field: str) -> str:
    url = _text(value, field, required=True, limit=2048)
    try:
        safe_url = validate_public_http_url(url)
        parsed = urlsplit(safe_url)
        port = parsed.port
    except ValueError as exc:
        raise DecisionError(f"{field} is not a valid public URL: {exc}") from exc
    if port not in {None, 80, 443}:
        raise DecisionError(f"{field} uses a non-standard port")
    return safe_url


def _require_list(decision: Mapping[str, Any], name: str, limit: int) -> list[Any]:
    value = decision.get(name)
    if not isinstance(value, list):
        raise DecisionError(f"{name} must be an array")
    if len(value) > limit:
        raise DecisionError(f"{name} may contain at most {limit} entries")
    return value


def validate_decision(
    raw: Mapping[str, Any],
    *,
    expected_product_id: str,
    expected_lease_token: str,
    minimum_confidence: float = 0.85,
    minimum_fact_confidence: float = 0.8,
    mirrors_allowed: bool = False,
    allowed_evidence_urls: set[str] | None = None,
) -> dict[str, Any]:
    """Return a normalized decision or reject it before any Wiki mutation."""

    if not isinstance(raw, Mapping):
        raise DecisionError("decision must be a JSON object")
    unknown = set(raw) - _TOP_LEVEL
    if unknown:
        raise DecisionError(f"decision has unknown fields: {', '.join(sorted(unknown))}")
    if raw.get("schema_version") != "1":
        raise DecisionError("schema_version must be '1'")

    product_id = _text(raw.get("product_id"), "product_id", required=True, limit=200)
    lease_token = _text(raw.get("lease_token"), "lease_token", required=True, limit=200)
    if product_id != expected_product_id:
        raise DecisionError("product_id does not match the active lease")
    if lease_token != expected_lease_token:
        raise DecisionError("lease_token does not match the active lease")

    outcome = raw.get("outcome")
    if outcome not in OUTCOMES:
        raise DecisionError("outcome is invalid")
    confidence = _confidence(raw.get("confidence"), "confidence")

    datasheets: list[dict[str, Any]] = []
    for index, item in enumerate(_require_list(raw, "datasheets", 10)):
        if not isinstance(item, Mapping):
            raise DecisionError(f"datasheets[{index}] must be an object")
        unknown_item = set(item) - {"url", "title", "source_type", "is_primary"}
        if unknown_item:
            raise DecisionError(f"datasheets[{index}] has unknown fields")
        source_type = item.get("source_type")
        if source_type not in SOURCE_TYPES:
            raise DecisionError(f"datasheets[{index}].source_type is invalid")
        if not isinstance(item.get("is_primary"), bool):
            raise DecisionError(f"datasheets[{index}].is_primary must be boolean")
        datasheets.append(
            {
                "url": validate_public_url(item.get("url"), f"datasheets[{index}].url"),
                "title": _text(
                    item.get("title"), f"datasheets[{index}].title", required=True, limit=500
                ),
                "source_type": source_type,
                "is_primary": item["is_primary"],
            }
        )

    sources: list[dict[str, Any]] = []
    for index, item in enumerate(_require_list(raw, "sources", 20)):
        if not isinstance(item, Mapping):
            raise DecisionError(f"sources[{index}] must be an object")
        unknown_item = set(item) - {"url", "title", "source_type"}
        if unknown_item:
            raise DecisionError(f"sources[{index}] has unknown fields")
        source_type = item.get("source_type")
        if source_type not in SOURCE_TYPES:
            raise DecisionError(f"sources[{index}].source_type is invalid")
        sources.append(
            {
                "url": validate_public_url(item.get("url"), f"sources[{index}].url"),
                "title": _text(
                    item.get("title"), f"sources[{index}].title", required=True, limit=500
                ),
                "source_type": source_type,
            }
        )

    declared_urls = {item["url"] for item in datasheets + sources}
    if allowed_evidence_urls is not None:
        normalized_allowed = {
            validate_public_url(url, "allowed_evidence_urls")
            for url in allowed_evidence_urls
        }
        undeclared = declared_urls - normalized_allowed
        if undeclared:
            raise DecisionError("decision cites URLs not extracted for this lease")

    summary = _text(raw.get("summary"), "summary", limit=2000)
    product_category = canonical_product_category(
        _text(raw.get("product_category"), "product_category", limit=100)
    )
    review_summary = _text(
        raw.get("review_summary"), "review_summary", limit=1500
    )
    review_evidence_raw = raw.get("review_evidence_urls", [])
    if not isinstance(review_evidence_raw, list) or len(review_evidence_raw) > 5:
        raise DecisionError("review_evidence_urls must be an array of at most 5 URLs")
    review_evidence_urls = [
        validate_public_url(url, "review_evidence_urls")
        for url in review_evidence_raw
    ]
    if review_summary and len(set(review_evidence_urls)) < 2:
        raise DecisionError(
            "review_summary requires at least two review evidence URLs"
        )
    if review_evidence_urls and not review_summary:
        raise DecisionError("review_evidence_urls require review_summary")
    if not set(review_evidence_urls) <= declared_urls:
        raise DecisionError(
            "review_evidence_urls must reference declared sources"
        )

    source_types_by_url = {
        item["url"]: item["source_type"] for item in datasheets + sources
    }
    community_urls = {
        url for url, source_type in source_types_by_url.items()
        if source_type == "community"
    }
    if any(item["source_type"] == "community" for item in datasheets):
        raise DecisionError("community sources cannot be datasheets")
    if not community_urls <= set(review_evidence_urls):
        raise DecisionError(
            "community sources may be cited only as review evidence"
        )

    facts: list[dict[str, Any]] = []
    for index, item in enumerate(_require_list(raw, "facts", 100)):
        if not isinstance(item, Mapping):
            raise DecisionError(f"facts[{index}] must be an object")
        unknown_item = set(item) - {
            "name",
            "category",
            "value",
            "unit",
            "confidence",
            "evidence_urls",
        }
        if unknown_item:
            raise DecisionError(f"facts[{index}] has unknown fields")
        value = item.get("value")
        if isinstance(value, (dict, list)) or value is None:
            raise DecisionError(f"facts[{index}].value must be scalar")
        evidence_raw = item.get("evidence_urls")
        if not isinstance(evidence_raw, list) or not evidence_raw:
            raise DecisionError(f"facts[{index}].evidence_urls must be non-empty")
        evidence = [
            validate_public_url(url, f"facts[{index}].evidence_urls")
            for url in evidence_raw
        ]
        fact_confidence = _confidence(
            item.get("confidence"), f"facts[{index}].confidence"
        )
        if fact_confidence < minimum_fact_confidence:
            raise DecisionError(
                f"facts[{index}].confidence is below the configured threshold"
            )
        if not set(evidence) <= declared_urls:
            raise DecisionError(
                f"facts[{index}].evidence_urls must reference declared sources"
            )
        if set(evidence) & community_urls:
            raise DecisionError(
                f"facts[{index}] cannot use community review evidence"
            )
        fact = {
            "name": _text(item.get("name"), f"facts[{index}].name", required=True, limit=200),
            "value": value,
            "confidence": fact_confidence,
            "evidence_urls": evidence,
        }
        if "unit" in item:
            fact["unit"] = _text(item.get("unit"), f"facts[{index}].unit", limit=80)
        if "category" in item:
            fact["category"] = _text(
                item.get("category"), f"facts[{index}].category", limit=100
            )
        facts.append(fact)

    conflicts = _require_list(raw, "conflicts", 100)
    normalized_conflicts: list[dict[str, Any]] = []
    conflict_fields: set[str] = set()
    for index, item in enumerate(conflicts):
        if not isinstance(item, Mapping):
            raise DecisionError(f"conflicts[{index}] must be an object")
        unknown_item = set(item) - {"field", "values", "source_urls"}
        if unknown_item:
            raise DecisionError(f"conflicts[{index}] has unknown fields")
        field = _text(
            item.get("field"), f"conflicts[{index}].field", required=True, limit=200
        )
        conflict_fields.add(field.casefold())
        if not isinstance(item.get("values"), list) or len(item["values"]) < 2:
            raise DecisionError(f"conflicts[{index}].values needs at least two values")
        if any(isinstance(value, (dict, list)) or value is None for value in item["values"]):
            raise DecisionError(f"conflicts[{index}].values must contain scalars")
        urls = item.get("source_urls")
        if not isinstance(urls, list) or not urls:
            raise DecisionError(f"conflicts[{index}].source_urls must be non-empty")
        normalized_urls = [
            validate_public_url(url, f"conflicts[{index}].source_urls") for url in urls
        ]
        if not set(normalized_urls) <= declared_urls:
            raise DecisionError(
                f"conflicts[{index}].source_urls must reference declared sources"
            )
        normalized_conflicts.append(
            {"field": field, "values": list(item["values"]), "source_urls": normalized_urls}
        )

    disputed_facts = {item["name"].casefold() for item in facts} & conflict_fields
    if disputed_facts:
        raise DecisionError("conflicted fields must not also appear as verified facts")

    if outcome == "publish":
        if confidence < minimum_confidence:
            raise DecisionError("publish confidence is below the configured threshold")
        if not product_category:
            raise DecisionError(
                "publish requires a public product_category; internal family codes "
                "must not be used"
            )
        if not summary:
            raise DecisionError("publish requires a user-facing product summary")
        if len(facts) < 5:
            raise DecisionError(
                "publish requires at least 5 cited specification facts"
            )
        primary = [item for item in datasheets if item["is_primary"]]
        if not primary:
            raise DecisionError("publish requires a primary datasheet")
        if not any(item["source_type"] in TRUSTED_TYPES for item in primary):
            mirror_domains = {
                ".".join((urlsplit(item["url"]).hostname or "").split(".")[-2:])
                for item in primary
                if item["source_type"] == "mirror"
            }
            if (
                not mirrors_allowed
                or len(primary) < 2
                or len(mirror_domains) < 2
                or any(item["source_type"] != "mirror" for item in primary)
            ):
                raise DecisionError(
                    "primary datasheet needs a trusted source or two enabled independent mirrors"
                )

    return {
        "schema_version": "1",
        "product_id": product_id,
        "lease_token": lease_token,
        "outcome": outcome,
        "confidence": confidence,
        "manufacturer": _text(raw.get("manufacturer"), "manufacturer", limit=300),
        "model": _text(raw.get("model"), "model", limit=300),
        "product_category": product_category,
        "summary": summary,
        "review_summary": review_summary,
        "review_evidence_urls": review_evidence_urls,
        "decision_notes": _text(
            raw.get("decision_notes"), "decision_notes", required=True, limit=1000
        ),
        "datasheets": datasheets,
        "sources": sources,
        "facts": facts,
        "conflicts": normalized_conflicts,
    }


__all__ = [
    "DecisionError",
    "canonical_product_category",
    "validate_decision",
    "validate_public_url",
]
